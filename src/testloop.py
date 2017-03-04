from settings import *

import math
import os
import threading
import socket
import json
import time
from subprocess import call
#from multiprocessing import cpu_count
from datetime import datetime as dt

import numpy as np
import quaternion
from astropy.coordinates import spherical_to_cartesian

from tools import (spherical_to_q, q_times_v, q_to_unitbase, normalize_v,
                   wrap_rads, solar_elongation)
from algorithm import CentroidAlgo, PositioningException

VISIT_OUTFILE_PREFIX = "visitout"

class TestLoop():
    def __init__(self, window):
        self.window = window
        self.exit = False
        self._sock = None
        self._algorithm_finished = None
        
        def handle_close():
            self.exit = True
            if self._algorithm_finished:
                self._algorithm_finished.set()
            
        self.window.closing.append(handle_close)
        
    def _maybe_exit(self):
        if self.exit:
            print('Exiting...')
            self._cleanup()
            quit()
        
    def run(self, times, log_prefix='test-', cleanup=True, **kwargs):
        # write logfile header
        logbody = log_prefix + dt.now().strftime('%Y%m%d-%H%M%S')
        imgdir = os.path.join(LOG_DIR, logbody)
        os.mkdir(imgdir)
        
        logfile = LOG_DIR + logbody + '.log'
        with open(logfile, 'w') as file:
            file.write('\t'.join((
                'iter', 'date', 'execution time',
                'time', 'sc pos x', 'sc pos y', 'sc pos z',
                'sc lat', 'sc lon', 'sc rot', 'ax lat', 'ax lon',
                'sol elong', 'light dir', 'imgfile',
                'meas time', 'meas sc lat', 'meas sc lon', 'meas sc rot',
                'est sc pos x', 'est sc pos y', 'est sc pos z',
                'err sc pos x', 'err sc pos y', 'err sc pos z',
                'abs error', 'rel error'
            ))+'\n')

        ex_times, errs, rerrs, li = [], [], [], 0
        for i in range(times):
            self._maybe_exit()
            
            etime, params, measure, pos, err = self._mainloop(imgdir, **kwargs)

            # print out progress
            if math.floor(100*i/times) > li:
                print('.', end='', flush=True)
                li += 1

            # calculate distance error
            if not math.isnan(err[0]):
                errd = math.sqrt(sum(map(lambda x: x**2, err)))
                errs.append(errd)
                rerrs.append(errd/abs(params[3])) # divided by distance
            else:
                errd = float('nan')

            ex_times.append(etime)

            # log all parameter values, timing & errors into a file
            with open(logfile, 'a') as file:
                file.write('\t'.join(map(str, (
                    i, dt.now().strftime("%Y-%m-%d %H:%M:%S"), etime,
                    *params, *measure, *pos, *err, errd, errd/abs(params[3])
                )))+'\n')

        try:
            qtles = ', '.join('%.2f'%(100*np.nanpercentile(rerrs, q),) for q in (2,10,25,50,75,90,98))
        except Exception as e:
            print('Error calculating quantiles: %s'%e)
            qtles = 'error'
        
        # a summary line
        summary = (
            '%s - time: %.1fh (%.0fs), '
            +'err avg: %.0fkm, rel-err avg: %.2f%%, '
            +'rel-err qs%%: (%s)\n'
        ) % (
            dt.now().strftime("%Y-%m-%d %H:%M:%S"),
            sum(ex_times)/3600,
            sum(ex_times)/times,
            sum(errs)/times,
            100*sum(rerrs)/times,
            qtles,
        )
        
        with open(logfile, 'r') as org: data = org.read()
        with open(logfile, 'w') as mod: mod.write(summary + data)
        print("\n" + summary)
        
        if cleanup:
            self._cleanup()
    

    def _mainloop(self, imgdir, **kwargs):
        start_time = dt.now()
        sm = self.window.systemModel

        while True:
            ## sample params from suitable distributions
            ##
            # datetime dist: uniform, based on rotation period
            time = np.random.uniform(*sm.time.range)

            # spacecraft position relative to asteroid in ecliptic coords:
            sc_lat = np.random.uniform(-math.pi/2, math.pi/2)
            sc_lon = np.random.uniform(-math.pi, math.pi)

            # s/c distance as inverse uniform distribution
            max_r, min_r = map(abs, sm.z_off.range)
            sc_r = 1/np.random.uniform(1/max_r, 1/min_r)

            # same in cartesian coord
            sc_ex_u, sc_ey_u, sc_ez_u = spherical_to_cartesian(sc_r, sc_lat, sc_lon)
            sc_ex, sc_ey, sc_ez = sc_ex_u.value, sc_ey_u.value, sc_ez_u.value

            # s/c to asteroid vector
            sc_ast_v = -np.array([sc_ex, sc_ey, sc_ez])

            # sc orientation: uniform, center of asteroid at edge of screen - some margin
            da = np.random.uniform(0, math.radians(CAMERA_FIELD_OF_VIEW/2))
            dd = np.random.uniform(0, 2*math.pi)
            sco_lat = wrap_rads(-sc_lat + da*math.sin(dd))
            sco_lon = wrap_rads(math.pi + sc_lon + da*math.cos(dd))
            sco_rot = np.random.uniform(-math.pi, math.pi) # rotation around camera axis
            sco_q = spherical_to_q(sco_lat, sco_lon, sco_rot)
            
            # sc_ast_p ecliptic => sc_ast_p open gl -z aligned view
            sc_x, sc_y, sc_z = q_times_v((sco_q * sm.q_sc2gl).conj(), sc_ast_v)
            
            # asteroid rotation axis, add zero mean gaussian with small variance
            da = np.random.uniform(0, math.radians(0.1))
            dd = np.random.uniform(0, 2*math.pi)
            ax_lat_true = sm.asteroid.axis_latitude
            ax_lon_true = sm.asteroid.axis_longitude
            ax_lat = ax_lat_true + da*math.sin(dd)
            ax_lon = ax_lon_true + da*math.cos(dd)
            ast_q = sm.asteroid.rotation_q(time)
            
            # get asteroid position so that know where sun is
            # *actually barycenter, not sun
            as_x, as_y, as_z = as_v = sm.asteroid.position(time)
            elong, direc = solar_elongation(as_v, sco_q)

            # limit elongation to always be more than 15deg
            if math.degrees(elong) > 15:
                break
            
        
        ## add measurement noise to
        # - datetime (95% within +-10s)
        meas_time = time + np.random.normal(0, 10/2)

        # - spacecraft orientation measure (95% within +-0.1 deg)
        meas_sco_lat = sco_lat + np.random.normal(0, math.radians(0.1)/2)
        meas_sco_lat = max(-math.pi/2, min(math.pi/2, meas_sco_lat))
        meas_sco_lon = wrap_rads(sco_lon + np.random.normal(0, math.radians(0.1)/2))
        meas_sco_rot = wrap_rads(sco_rot + np.random.normal(0, math.radians(0.1)/2))


        ## based on above, call VISIT to make a target image
        ##
        light = normalize_v(q_times_v(ast_q.conj(), np.array([as_x, as_y, as_z])))
        focus = q_times_v(ast_q.conj(), np.array([sc_ex, sc_ey, sc_ez]))
        view_x, ty, view_z = q_to_unitbase(ast_q.conj() * sco_q)

        # in VISIT focus & view_normal are vectors pointing out from the object,
        # light however points into the object
        view_x = -view_x
        focus += -23*view_x     # 23km bias in view_normal direction?!

        # all params VISIT needs
        visit_params = {
            'out_file':         VISIT_OUTFILE_PREFIX,
            'out_dir':          imgdir.replace('\\', '\\\\'),
            'view_angle':       CAMERA_FIELD_OF_VIEW,
            'out_width':        CAMERA_WIDTH,
            'out_height':       CAMERA_HEIGHT,
            'max_distance':     -sm.z_off.range[0]+10,
            'light_direction':  tuple(light),    # vector into the object
            'focus':            tuple(focus),    # vector from object to camera
            'view_normal':      tuple(view_x),   # reverse camera borehole direction
            'up_vector':        tuple(view_z),   # camera up direction
        }

        # call VISIT
        imgfile = self._render(visit_params)
        self._maybe_exit()

        if False:
            tx, ty, tz = q_times_v((sco_q).conj(), sc_ast_v)
            print('%s'%''.join('%s: %s\n'%(k,v) for k,v in (
                ('ast_x_v', list(q_times_v(ast_q, np.array([1, 0, 0])))),
                ('sc_lat', sc_lat),
                ('sc_lon', sc_lon),
                ('sco_lat', sco_lat),
                ('sco_lon', sco_lon),
                ('sco_rot', sco_rot),
                ('ec_ast_sc_v', [sc_ex, sc_ey, sc_ez]),
                ('sc_sc_ast_v', [tx, ty, tz]),
                ('gl_sc_ast_v', [sc_x, sc_y, sc_z]),
            )))

        # set datetime, spacecraft & asteroid orientation
        sm.time.value = meas_time
        sm.x_rot.value = math.degrees(meas_sco_lat)
        sm.y_rot.value = math.degrees(meas_sco_lon)
        sm.z_rot.value = math.degrees(meas_sco_rot)
        sm.asteroid.axis_latitude = ax_lat
        sm.asteroid.axis_longitude = ax_lon
        sm.real_sc_pos = [sc_x, sc_y, sc_z]

        # load image & run optimization algo(s)
        ok = self._run_algo(imgfile, **kwargs)
        self._maybe_exit()

        if False:
            # DEBUG ONLY: for plotting in own software for comparison
            sm.time.value = time
            sm.x_rot.value = math.degrees(sco_lat)
            sm.y_rot.value = math.degrees(sco_lon)
            sm.z_rot.value = math.degrees(sco_rot)
            sm.asteroid.axis_latitude = ax_lat_true
            sm.asteroid.axis_longitude = ax_lon_true
            sm.set_spacecraft_pos((sc_x, sc_y, sc_z))

        # assemble return values
        etime = (dt.now() - start_time).total_seconds()
        params = (time, sc_x, sc_y, sc_z, sco_lat, sco_lon, sco_rot, ax_lat,
                ax_lon, math.degrees(elong), math.degrees(direc), imgfile)
        measure = (meas_time, meas_sco_lat, meas_sco_lon, meas_sco_rot)
        pos = (sm.x_off.value, sm.y_off.value, sm.z_off.value)
        err = (sm.x_off.value - sc_x, sm.y_off.value - sc_y, sm.z_off.value - sc_z)
        if not ok:
            err = pos = float('nan')*np.ones(3)

        return etime, params, measure, pos, err
    
    
    def _run_algo(self, imgfile_, **kwargs):
        self._algorithm_finished = threading.Event()
        def run_this_from_qt_thread(glWidget, imgfile, **kwargs):
            glWidget.loadTargetImage(imgfile, CAMERA_HEIGHT)
            try:
                CentroidAlgo.update_sc_pos(glWidget.systemModel, glWidget.image)
                ok = True
            except PositioningException as e:
                if not e.args or e.args[0] != 'No asteroid found':
                    print(str(e))
                print('|', end='', flush=True)
                ok = False

            if kwargs.get('method', False) and ok:
                glWidget.parent().optim.findstate(**kwargs)

            imgfile = imgfile[0:-4]+'r'+'.png'
            glWidget.saveViewToFile(imgfile)
            self._algorithm_finished.set()
            return ok
        
        self.window.tsRun.emit((
            run_this_from_qt_thread,
            (self.window.glWidget, imgfile_),
            kwargs
        ))
        
        self._algorithm_finished.wait()
        return self.window.tsRunResult
        
    
    def _cleanup(self):
        self._send('quit')

    def _render(self, params):
        ok = False
        for i in range(3):
            self._send(json.dumps(params))
            imgfile = self._receive()
            if len(imgfile)>0:
                break
        
        if len(imgfile)==0:
            raise RuntimeError("Cant connect to VISIT")
        
        return imgfile
        

    def _connect(self):
        if self._sock is None:
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        
        try:
            res = self._sock.connect(('127.0.0.1', VISIT_PORT))
        except ConnectionRefusedError:
            self._visit_th = VisitThread()
            self._visit_th.start()
            
            for i in range(3):
                time.sleep(10)
                try:
                    self._sock.connect(('127.0.0.1', VISIT_PORT))
                    ok = True
                except ConnectionRefusedError:
                    ok = False
                if ok:
                    break
                
            if not ok:
                raise RuntimeError("Cant connect to VISIT")
    
    def _send(self, msg):
        bmsg = msg.encode('utf-8')
        totalsent = 0
        while totalsent < len(bmsg):
            sent = self._sock.send(bmsg[totalsent:]) \
                    if self._sock is not None else 0
            totalsent = totalsent + sent
            if sent == 0:
                self._connect()
                totalsent = 0
                
        self._sock.shutdown(1)
        
    def _receive(self):
        imgfile = self._sock.recv(256)
        self._sock.close()
        self._sock = None
        return imgfile.decode('utf-8')


class VisitThread(threading.Thread):
    def __init__(self):
        super(VisitThread, self).__init__()
        self.threadID = 2
        self.name = 'visit-thread'
        self.counter = 2
        self.window = None
        
    def run(self):
        # -nowin crashes VISIT
        call(['visit', '-cli', '-l', 'srun', '-np', '1',
                '-s', VISIT_SCRIPT_PY_FILE])
                
#        call(['visit', '-cli', '-l', 'srun', '-np', '%d'%int(cpu_count()/2),
#               '-s', VISIT_SCRIPT_PY_FILE]) # multiple threads seemed slower