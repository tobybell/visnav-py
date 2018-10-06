import time

import cv2

from algo.centroid import CentroidAlgo
from algo.image import ImageProc
from algo.keypoint import KeypointAlgo
from algo.mixed import MixedAlgo
from algo.phasecorr import PhaseCorrelationAlgo
from render.render import RenderEngine
from settings import *

import math
from math import degrees as deg
from math import radians as rad
import os
import sys
import shutil
import pickle
import threading
from datetime import datetime as dt
from decimal import *

import numpy as np
import quaternion
from astropy.coordinates import spherical_to_cartesian

from iotools import objloader
from iotools import lblloader

from algo.model import Asteroid, SystemModel
import algo.tools as tools
from algo.tools import (ypr_to_q, q_to_ypr, q_times_v, q_to_unitbase, normalize_v,
                   wrap_rads, solar_elongation, angle_between_ypr)
from algo.tools import PositioningException

#from memory_profiler import profile
#import tracemalloc
#import gc
# TODO: fix suspected memory leaks at
#   - quaternion.as_float_array (?)
#   - cv2.solvePnPRansac (ref_kp_3d?)
#   - astropy, many places


class TestLoop():
    FILE_PREFIX = 'iteration_'

    def __init__(self, far=False):
        self.exit = False
        self._algorithm_finished = None
        self._smooth_faces = False

        if far:
            TestLoop.FILE_PREFIX = 'far'
            self.max_r = MAX_DISTANCE
            self.min_r = MAX_MED_DISTANCE
        else:
            self.max_r = MAX_MED_DISTANCE
            self.min_r = MIN_MED_DISTANCE

        self.system_model = SystemModel()
        self.render_engine = RenderEngine(VIEW_WIDTH, VIEW_HEIGHT)
        self.obj_idx = self.render_engine.load_object(self.system_model.real_shape_model, smooth=self._smooth_faces)

        self.keypoint = KeypointAlgo(self.system_model, self.render_engine, self.obj_idx)
        self.centroid = CentroidAlgo(self.system_model, self.render_engine, self.obj_idx)
        self.phasecorr = PhaseCorrelationAlgo(self.system_model, self.render_engine, self.obj_idx)
        self.mixedalgo = MixedAlgo(self.centroid, self.keypoint)

        # init later if needed
        self._synth_navcam = None
        self._hires_obj_idx = None

        # gaussian sd in seconds
        self._noise_time = 30/2     # 95% within +-30s
        
        # uniform, max dev in deg
        self._noise_ast_rot_axis = 10
        self._noise_ast_phase_shift = 10/2  # 95% within 10 deg

        # s/c orientation noise, gaussian sd in deg
        self._noise_sco_lat = 2/2   # 95% within 2 deg
        self._noise_sco_lon = 2/2   # 95% within 2 deg
        self._noise_sco_rot = 2/2   # 95% within 2 deg
        
        # minimum allowed elongation in deg
        self._min_elong = 45
        
        # transients
        self._smn_cache_id = ''
        self._iter_dir = None
        self._logfile = None
        self._fval_logfile = None
        self._run_times = []
        self._laterrs = []
        self._disterrs = []
        self._roterrs = []
        self._shifterrs = []
        self._fails = 0        
        self._timer = None
        self._L = None
        self._state_list = None
        self._rotation_noise = None

        def handle_close():
            self.exit = True
            if self._algorithm_finished:
                self._algorithm_finished.set()


    # main method
    def run(self, times, log_prefix='test-', smn_type='',
            state_db_path=None, rotation_noise=True, **kwargs):
        self._smn_cache_id = smn_type
        self._state_db_path = state_db_path
        self._rotation_noise = rotation_noise
        
        skip = 0
        if isinstance(times, str):
            if ':' in times:
                skip, times = map(int, times.split(':'))
            else:
                times = int(times)
        
        if state_db_path is not None:
            n = self._init_state_db()
            times = min(n, times)
        
        # write logfile header
        self._init_log(log_prefix)
        
        li = 0
        sm = self.system_model
        
        for i in range(skip, times):
            #print('%s'%self._state_list[i])
            
            # maybe generate new noise for shape model
            sm_noise = 0
            if ADD_SHAPE_MODEL_NOISE:
                sm_noise = self.load_noisy_shape_model(sm, i)
                if sm_noise is None:
                    if DEBUG:
                        print('generating new noisy shape model')
                    sm_noise = self.generate_noisy_shape_model(sm, i)
                    self._maybe_exit()

            # try to load system state
            initial = self.load_state(sm, i) if self._rotation_noise else None
            if initial or self._state_list:
                # successfully loaded system state,
                # try to load related navcam image
                imgfile = self.load_navcam_image(i)
            else:
                imgfile = None

            if initial is None:
                if DEBUG:
                    print('generating new state')
                
                # generate system state
                self.generate_system_state(sm, i)

                # add noise to current state, wipe sc pos
                initial = self.add_noise(sm)

                # save state to lbl file
                if self._rotation_noise:
                    sm.save_state(self._cache_file(i))
            
            # maybe new system state or no previous image, if so, render
            if imgfile is None:
                if DEBUG:
                    print('generating new navcam image')
                imgfile = self.render_navcam_image(sm, i)
                self._maybe_exit()
            
            # run algorithm
            ok, rtime = self._run_algo(imgfile, self._iter_file(i), **kwargs)
            
            if kwargs.get('use_feature_db', False) and kwargs.get('add_noise', False):
                sm_noise = self.keypoint.sm_noise
            
            # calculate results
            results = self.calculate_result(sm, i, imgfile, ok, initial, **kwargs)
            
            # write log entry
            self._write_log_entry(i, rtime, sm_noise, *results)
            self._maybe_exit()

            # print out progress
            if DEBUG:
                print('\niteration i=%d:'%(i+1), flush=True)
            elif math.floor(100*i/(times - skip)) > li:
                print('.', end='', flush=True)
                li += 1

        self._close_log(times-skip)


    def generate_system_state(self, sm, i):
        # reset asteroid axis to true values
        sm.asteroid = Asteroid()
        sm.asteroid_rotation_from_model()
        
        if self._state_list is not None:
            lblloader.load_image_meta(
                os.path.join(self._state_db_path, self._state_list[i]+'.LBL'), sm)

            return
        
        for i in range(100):
            ## sample params from suitable distributions
            ##
            # datetime dist: uniform, based on rotation period
            time = np.random.uniform(*sm.time.range)

            # spacecraft position relative to asteroid in ecliptic coords:
            sc_lat = np.random.uniform(-math.pi/2, math.pi/2)
            sc_lon = np.random.uniform(-math.pi, math.pi)

            # s/c distance as inverse uniform distribution
            sc_r = 1/np.random.uniform(1/self.max_r, 1/self.min_r)

            # same in cartesian coord
            sc_ex_u, sc_ey_u, sc_ez_u = spherical_to_cartesian(sc_r, sc_lat, sc_lon)
            sc_ex, sc_ey, sc_ez = sc_ex_u.value, sc_ey_u.value, sc_ez_u.value

            # s/c to asteroid vector
            sc_ast_v = -np.array([sc_ex, sc_ey, sc_ez])

            # sc orientation: uniform, center of asteroid at edge of screen - some margin
            da = np.random.uniform(0, rad(CAMERA_Y_FOV/2))
            dd = np.random.uniform(0, 2*math.pi)
            sco_lat = wrap_rads(-sc_lat + da*math.sin(dd))
            sco_lon = wrap_rads(math.pi + sc_lon + da*math.cos(dd))
            sco_rot = np.random.uniform(-math.pi, math.pi) # rotation around camera axis
            sco_q = ypr_to_q(sco_lat, sco_lon, sco_rot)
            
            # sc_ast_p ecliptic => sc_ast_p open gl -z aligned view
            sc_pos = q_times_v((sco_q * sm.sc2gl_q).conj(), sc_ast_v)
            
            # get asteroid position so that know where sun is
            # *actually barycenter, not sun
            as_v = sm.asteroid.position(time)
            elong, direc = solar_elongation(as_v, sco_q)

            # limit elongation to always be more than set elong
            if elong > rad(self._min_elong):
                break
        
        if elong <= rad(self._min_elong):
            assert False, 'probable infinite loop'
        
        # put real values to model
        sm.time.value = time
        sm.spacecraft_pos = sc_pos
        sm.spacecraft_rot = (deg(sco_lat), deg(sco_lon), deg(sco_rot))

        # save real values so that can compare later
        sm.time.real_value = sm.time.value
        sm.real_spacecraft_pos = sm.spacecraft_pos
        sm.real_spacecraft_rot = sm.spacecraft_rot
        sm.real_asteroid_axis = sm.asteroid_axis

        # get real relative position of asteroid model vertices
        sm.real_sc_ast_vertices = sm.sc_asteroid_vertices()
        
        
    def add_noise(self, sm):
        rotation_noise = True if self._state_list is None else self._rotation_noise
        
        ## add measurement noise to
        # - datetime (seconds)
        if rotation_noise:
            meas_time = sm.time.real_value + np.random.normal(0, self._noise_time)
            sm.time.value = meas_time
            assert np.isclose(sm.time.value, meas_time), 'Failed to set time value'

        # - asteroid state estimate
        ax_lat, ax_lon, ax_phs = map(rad, sm.real_asteroid_axis)
        noise_da = np.random.uniform(0, rad(self._noise_ast_rot_axis))
        noise_dd = np.random.uniform(0, 2*math.pi)
        meas_ax_lat = ax_lat + noise_da*math.sin(noise_dd)
        meas_ax_lon = ax_lon + noise_da*math.cos(noise_dd)
        meas_ax_phs = ax_phs + np.random.normal(0, rad(self._noise_ast_phase_shift))
        if rotation_noise:
            sm.asteroid_axis = map(deg, (meas_ax_lat, meas_ax_lon, meas_ax_phs))

        # - spacecraft orientation measure
        sc_lat, sc_lon, sc_rot = map(rad, sm.real_spacecraft_rot)
        meas_sc_lat = max(-math.pi/2, min(math.pi/2, sc_lat
                + np.random.normal(0, rad(self._noise_sco_lat))))
        meas_sc_lon = wrap_rads(sc_lon 
                + np.random.normal(0, rad(self._noise_sco_lon)))
        meas_sc_rot = wrap_rads(sc_rot
                + np.random.normal(0, rad(self._noise_sco_rot)))
        if rotation_noise:
            sm.spacecraft_rot = map(deg, (meas_sc_lat, meas_sc_lon, meas_sc_rot))
        
        # wipe spacecraft position clean
        sm.spacecraft_pos = (0, 0, -MIN_MED_DISTANCE)

        # return this initial state
        return self._initial_state(sm)
        
    def _initial_state(self, sm):
        return {
            'time': sm.time.value,
            'ast_axis': sm.asteroid_axis,
            'sc_rot': sm.spacecraft_rot,
        }
    
    def load_state(self, sm, i):
        try:
            sm.load_state(self._cache_file(i)+'.lbl', sc_ast_vertices=True)
            initial = self._initial_state(sm)
        except FileNotFoundError:
            initial = None
        return initial
        
        
    def generate_noisy_shape_model(self, sm, i):
        #sup = objloader.ShapeModel(fname=SHAPE_MODEL_NOISE_SUPPORT)
        noisy_model, sm_noise, self._L = \
                tools.apply_noise(sm.real_shape_model,
                                  #support=np.array(sup.vertices),
                                  L=self._L,
                                  len_sc=SHAPE_MODEL_NOISE_LEN_SC,
                                  noise_lv=SHAPE_MODEL_NOISE_LV)
        
        fname = self._cache_file(i, prefix='shapemodel_')+'_'+self._smn_cache_id+'.nsm'
        with open(fname, 'wb') as fh:
            pickle.dump((noisy_model.as_dict(), sm_noise), fh, -1)
        
        self.render_engine.load_object(noisy_model, self.obj_idx, smooth=self._smooth_faces)
        return sm_noise
    
    def load_noisy_shape_model(self, sm, i):
        try:
            fname = self._cache_file(i, prefix='shapemodel_')+'_'+self._smn_cache_id+'.nsm'
            with open(fname, 'rb') as fh:
                noisy_model, sm_noise = pickle.load(fh)
            self.render_engine.load_object(objloader.ShapeModel(data=noisy_model), self.obj_idx, smooth=self._smooth_faces)
        except (FileNotFoundError, EOFError):
            sm_noise = None
        return sm_noise

    def render_navcam_image(self, sm, i):
        if self._synth_navcam is None:
            self._synth_navcam = RenderEngine(CAMERA_WIDTH, CAMERA_HEIGHT, antialias_samples=16)
            self._synth_navcam.set_frustum(CAMERA_X_FOV, CAMERA_Y_FOV, 0.1, MAX_DISTANCE)
            self._hires_obj_idx = self._synth_navcam.load_object(HIRES_TARGET_MODEL_FILE)

        sm.swap_values_with_real_vals()
        pos = sm.spacecraft_pos
        q, _ = sm.gl_sc_asteroid_rel_q()
        light = sm.light_rel_dir()
        sm.swap_values_with_real_vals()

        img, depth = self._synth_navcam.render(self._hires_obj_idx, pos, q, light, get_depth=True, shadows=True)
        img = cv2.cvtColor(img, cv2.COLOR_RGB2GRAY)

        # coef=2 gives reasonably many stars
        img = ImageProc.add_stars(img.astype('float'), mask=depth>=MAX_DISTANCE-0.1, coef=2.5)

        # ratio seems too low but blurring in images match actual Rosetta navcam images
        img = ImageProc.apply_point_spread_fn(img, ratio=0.2)

        # add background noise
        img = ImageProc.add_ccd_noise(img, rate=6)
        img = np.clip(img, 0, 255).astype('uint8')

        if False:
            cv2.imshow('test', img)
            cv2.waitKey()
            quit()

        cache_file = self._cache_file(i)+'.png'
        cv2.imwrite(cache_file, img)
        return cache_file

    def load_navcam_image(self, i):
        if self._state_list is None:
            fname = self._cache_file(i)+'.png'
        else:
            fname = os.path.join(self._state_db_path, self._state_list[i]+'_P.png')
        return fname if os.path.isfile(fname) else None

    def calculate_result(self, sm, i, imgfile, ok, initial, **kwargs):
        # save function values from optimization
        fvals = self.window.phasecorr.optfun_values \
                if ok and kwargs.get('method', False)=='phasecorr' \
                else None
        final_fval = fvals[-1] if fvals else None

        real_rel_rot = q_to_ypr(sm.real_sc_asteroid_rel_q())
        elong, direc = sm.solar_elongation(real=True)
        r_ast_axis = sm.real_asteroid_axis
        
        # real system state
        params = (sm.time.real_value, *r_ast_axis,
                *sm.real_spacecraft_rot, deg(elong), deg(direc),
                *sm.real_spacecraft_pos, *map(deg, real_rel_rot),
                imgfile, final_fval)
        
        # calculate added noise
        #
        getcontext().prec = 6
        time_noise = float(Decimal(initial['time']) - Decimal(sm.time.real_value))
        
        ast_rot_noise = (
            initial['ast_axis'][0]-r_ast_axis[0],
            initial['ast_axis'][1]-r_ast_axis[1],
            360*time_noise/sm.asteroid.rotation_period
                + (initial['ast_axis'][2]-r_ast_axis[2])
        )
        sc_rot_noise = tuple(np.subtract(initial['sc_rot'], sm.real_spacecraft_rot))
        
        dev_angle = deg(angle_between_ypr(map(rad, ast_rot_noise),
                                          map(rad, sc_rot_noise)))
        
        noise = (time_noise,) + ast_rot_noise + sc_rot_noise + (dev_angle,)
        
        if not ok:
            pos = float('nan')*np.ones(3)
            rel_rot = float('nan')*np.ones(3)
            err = float('nan')*np.ones(4)
            
        else:
            pos = sm.spacecraft_pos
            rel_rot = q_to_ypr(sm.sc_asteroid_rel_q())
            est_vertices = sm.sc_asteroid_vertices()
            max_shift = tools.sc_asteroid_max_shift_error(
                    est_vertices, sm.real_sc_ast_vertices)
            
            err = (
                *np.subtract(pos, sm.real_spacecraft_pos),
                deg(angle_between_ypr(rel_rot, real_rel_rot)),
                max_shift,
            )
        
        return params, noise, pos, map(deg, rel_rot), fvals, err
    
    
    def _init_state_db(self):
        try:
            with open(os.path.join(self._state_db_path, 'ignore_these.txt'), 'rb') as fh:
                ignore = tuple(l.decode('utf-8').strip() for l in fh)
        except FileNotFoundError:
            ignore = tuple()
        self._state_list = sorted([f[:-4] for f in os.listdir(self._state_db_path)
                                          if f[-4:]=='.LBL' and f[:-4] not in ignore])
        return len(self._state_list)
    
    
    def _init_log(self, log_prefix):
        os.makedirs(LOG_DIR, exist_ok=True)
        os.makedirs(CACHE_DIR, exist_ok=True)
        
        logbody = log_prefix + dt.now().strftime('%Y%m%d-%H%M%S')
        self._iter_dir = os.path.join(LOG_DIR, logbody)
        os.mkdir(self._iter_dir)
        
        self._fval_logfile = LOG_DIR + logbody + '-fvals.log'
        self._logfile = LOG_DIR + logbody + '.log'
        with open(self._logfile, 'w') as file:
            file.write(' '.join(sys.argv)+'\n'+ '\t'.join((
                'iter', 'date', 'execution time',
                'time', 'ast lat', 'ast lon', 'ast rot',
                'sc lat', 'sc lon', 'sc rot', 
                'sol elong', 'light dir', 'x sc pos', 'y sc pos', 'z sc pos',
                'rel yaw', 'rel pitch', 'rel roll', 
                'imgfile', 'optfun val', 'shape model noise',
                'time dev', 'ast lat dev', 'ast lon dev', 'ast rot dev',
                'sc lat dev', 'sc lon dev', 'sc rot dev', 'total dev angle',
                'x est sc pos', 'y est sc pos', 'z est sc pos',
                'yaw rel est', 'pitch rel est', 'roll rel est',
                'x err sc pos', 'y err sc pos', 'z err sc pos', 'rot error',
                'shift error km', 'lat error', 'dist error', 'rel shift error',
            ))+'\n')
            
        self._run_times = []
        self._laterrs = []
        self._disterrs = []
        self._roterrs = []
        self._shifterrs = []
        self._fails = 0
        self._timer = tools.Stopwatch()
        self._timer.start()
        
        
    def _write_log_entry(self, i, rtime, sm_noise, params, noise, pos, rel_rot, fvals, err):

        # save execution time
        self._run_times.append(rtime)

        # calculate errors
        dist = abs(params[-6])
        if not math.isnan(err[0]):
            lerr = 1000*math.sqrt(err[0]**2 + err[1]**2) / dist     # m/km
            derr = 1000*abs(err[2]) / dist                          # m/km
            rerr = abs(err[3])
            serr = 1000*err[4] / dist                               # m/km
            self._laterrs.append(lerr)
            self._disterrs.append(derr)
            self._roterrs.append(rerr)
            self._shifterrs.append(serr)
        else:
            lerr = derr = rerr = serr = float('nan')
            self._fails += 1

        # log all parameter values, timing & errors into a file
        with open(self._logfile, 'a') as file:
            file.write('\t'.join(map(str, (
                i, dt.now().strftime("%Y-%m-%d %H:%M:%S"), rtime, *params,
                sm_noise, *noise, *pos, *rel_rot, *err, lerr, derr, serr
            )))+'\n')

        # log opt fun values in other file
        if fvals:
            with open(self._fval_logfile, 'a') as file:
                file.write('\t'.join(map(str, fvals))+'\n')
        
        
    def _close_log(self, times):
        if len(self._laterrs):
            prctls = (50, 68, 95) + ((99.7,) if times>=2000 else tuple())
            calc_prctls = lambda errs: \
                    ', '.join('%.2f' % p
                    for p in np.nanpercentile(errs, prctls))
            try:
                laterr_pctls = calc_prctls(self._laterrs)
                disterr_pctls = calc_prctls(self._disterrs)
                shifterr_pctls = calc_prctls(self._shifterrs)
                roterr_pctls = ', '.join(
                        ['%.2f'%p for p in np.nanpercentile(self._roterrs, prctls)])
            except Exception as e:
                print('Error calculating quantiles: %s'%e)
                laterr_pctls = 'error'
                disterr_pctls = 'error'
                shifterr_pctls = 'error'
                roterr_pctls = 'error'

            # a summary line
            summary_data = (
                sum(self._laterrs)/len(self._laterrs),
                laterr_pctls,
                sum(self._disterrs)/len(self._disterrs),
                disterr_pctls,
                sum(self._shifterrs)/len(self._shifterrs),
                shifterr_pctls,
                sum(self._roterrs)/len(self._roterrs),
                roterr_pctls,
            )
        else:
            summary_data = tuple(np.ones(8)*float('nan'))
        
        self._timer.stop()
        summary = (
            '%s - t: %.1fmin (%dms), '
            + 'Le: %.3f m/km (%s), '
            + 'De: %.3f m/km (%s), '
            + 'Se: %.3f m/km (%s), '
            + 'Re: %.3f° (%s), '
            + 'fail: %.2f%% \n'
        ) % (
            dt.now().strftime("%Y-%m-%d %H:%M:%S"),
            self._timer.elapsed/60,
            1000*np.nanmean(self._run_times) if len(self._run_times) else float('nan'),            
            *summary_data,
            100*self._fails/times,
        )
        
        with open(self._logfile, 'r') as org: data = org.read()
        with open(self._logfile, 'w') as mod: mod.write(summary + data)
        print("\n" + summary)

    def _run_algo(self, imgfile, outfile, **kwargs):
        ok, rtime = False, False
        timer = tools.Stopwatch()
        timer.start()
        method = kwargs.pop('method', False)
        if method == 'keypoint+':
            try:
                self.mixedalgo.run(imgfile, outfile, **kwargs)
                ok = True
            except PositioningException as e:
                print(str(e))
        if method == 'keypoint':
            try:
                # try using pympler to find memory leaks, fail: crashes always
                #    from pympler import tracker
                #    tr = tracker.SummaryTracker()
                self.keypoint.solve_pnp(imgfile, outfile, **kwargs)
                #    tr.print_diff()
                ok = True
                rtime = self.keypoint.timer.elapsed
            except PositioningException as e:
                print(str(e))
        elif method == 'centroid':
            try:
                self.centroid.adjust_iteratively(imgfile, outfile, **kwargs)
                ok = True
            except PositioningException as e:
                print(str(e))
        elif method == 'phasecorr':
            ok = self.phasecorr.findstate(imgfile, outfile, **kwargs)
        timer.stop()
        rtime = rtime if rtime else timer.elapsed
        return ok, rtime

    def _iter_file(self, i, prefix=None):
        if prefix is None:
            prefix = TestLoop.FILE_PREFIX
        return os.path.normpath(
                os.path.join(self._iter_dir, prefix+'%04d'%i))
    
    def _cache_file(self, i, prefix=None):
        if prefix is None:
            prefix = TestLoop.FILE_PREFIX
        return os.path.normpath(
            os.path.join(CACHE_DIR, (prefix+'%04d'%i) if self._state_list is None
                                    else self._state_list[i]))
    
    def _maybe_exit(self):
        if self.exit:
            print('Exiting...')
            quit()

