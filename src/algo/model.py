import sys
import math

import numpy as np
import quaternion # adds to numpy
from astropy.time import Time
from astropy import constants as const
from astropy import units
from astropy.coordinates import SkyCoord
import configparser

from iotools import objloader
from settings import *
from algo import tools


class Parameter():
    def __init__(self, min_val, max_val, def_val=None, estimate=True, is_gl_z=False):
        self._min_val = min_val
        self._max_val = max_val
        self.estimate = estimate
        self._def_val = def_val
        self._value = self.def_val
        self.is_gl_z = is_gl_z
        self.real_value = None
        self.change_callback = None
        self.fire_change_events = True
        self.debug=False
    
    @property
    def range(self):
        return (self._min_val, self._max_val)

    @range.setter
    def range(self, range):
        min_val, max_val = range

        # NOTE: need fine rtol as time is in seconds (e.g. 1407258438)
        if not np.isclose(self._min_val, min_val, rtol=1e-9) \
                or not np.isclose(self._max_val, max_val, rtol=1e-9):
            self._min_val = min_val
            self._max_val = max_val
            if self.fire_change_events:
                try:
                    self.change_callback(self._value, self._min_val, self._max_val)
                except TypeError:
                    pass

    @property
    def scale(self):
        return abs(self._max_val - self._min_val)
    
    @property
    def def_val(self):
        return (self._min_val + self._max_val)/2 \
                if self._def_val is None \
                else self._def_val

    @def_val.setter
    def def_val(self, def_val):
        self._def_val = def_val

    @property
    def value(self):
        return self._value

    @value.setter
    def value(self, value):
        if self.debug:
            print('o: %s, n: %s'%(self._value, value), flush=True)

        # NOTE: need fine rtol as time is in seconds (e.g. 1407258438)
        if not np.isclose(self._value, value, rtol=1e-9):
            self._value = value
            if self.debug:
                print('value set: %s'%self._value, flush=True)
            if self.fire_change_events:
                try:
                    self.change_callback(value)
                except TypeError:
                    pass
    
    @property
    def nvalue(self):
        if self.is_gl_z:
            scale = abs(1/self._min_val - 1/self._max_val)
            offset = (1/self._min_val + 1/self._max_val)/2
            return (-1/(self._value or 1e-6) + offset)/scale
        return (self._value - self.def_val)/self.scale
    
    @nvalue.setter
    def nvalue(self, nvalue):
        if self.is_gl_z:
            scale = abs(1/self._min_val - 1/self._max_val)
            offset = (1/self._min_val + 1/self._max_val)/2
            self.value = -1/((nvalue or 1e-6)*scale - offset)
        else:
            self.value = nvalue*self.scale + self.def_val
    
    def valid(self):
        return self._value >= self._min_val and self._value < self._max_val
    
    def __str__(self):
        return '%.2f (%.2f) in [%.2f, %.2f]' % (
            self._value,
            self.real_value if self.real_value is not None else float('nan'),
            self._min_val,
            self._max_val,
        )
    

class SystemModel():
    (
        OPENGL_FRAME,
        SPACECRAFT_FRAME,
        ASTEROID_FRAME,
        OPENCV_FRAME,
    ) = range(4)
    
    # from sc cam frame (axis: +x, up: +z) to opengl (axis -z, up: +y)
    sc2gl_q = np.quaternion(0.5, 0.5, -0.5, -0.5)

    # from ast frame (axis: +z, up: -x) to opengl (axis -z, up: +y)
    ast2gl_q = np.quaternion(1, 0, 0, -1).normalized()
    
    # from opencv cam frame (axis: +z, up: -y) to opengl (axis -z, up: +y)
    cv2gl_q = np.quaternion(0, 1, 0, 0)
    
    def __init__(self, *args, **kwargs):
        self.asteroid = Asteroid()
        self.real_shape_model = objloader.ShapeModel(fname=TARGET_MODEL_FILE)
        self.real_sc_ast_vertices = None
        
        # spacecraft position relative to asteroid, z towards spacecraft,
        #   x towards right when looking out from s/c camera, y up
        self.x_off = Parameter(-4, 4, estimate=False)
        self.y_off = Parameter(-4, 4, estimate=False)
        
        # whole view: 1.65km/tan(2.5deg) = 38km
        # can span ~30px: 1.65km/tan(2.5deg * 30/1024) = 1290km
        self.z_off = Parameter(-MAX_DISTANCE, -MIN_DISTANCE, def_val=-MIN_MED_DISTANCE, is_gl_z=True) # was 120, 220

        # spacecraft orientation relative to stars
        self.x_rot = Parameter(-90, 90, estimate=False) # axis latitude
        self.y_rot = Parameter(-180, 180, estimate=False) # axis longitude
        self.z_rot = Parameter(-180, 180, estimate=False) # rotation

        # asteroid zero orientation relative to stars
        self.ast_x_rot = Parameter(-90, 90, estimate=False) # axis latitude
        self.ast_y_rot = Parameter(-180, 180, estimate=False) # axis longitude
        self.ast_z_rot = Parameter(-180, 180, estimate=False) # rotation
        self.asteroid_rotation_from_model()

        # time in seconds since 1970-01-01 00:00:00
        self.time = Parameter(
            Time('2015-01-01 00:00:00').unix,
            Time('2015-01-01 00:00:00').unix + self.asteroid.rotation_period,
            estimate=False
        )
        
        # override any default params
        for n, v in kwargs.items():
            setattr(self, n, v)
        
        # set default values to params
        for n, p in self.get_params():
            p.value = p.def_val
        
    def get_params(self, all=False):
        return (
            (n, getattr(self, n))
            for n in sorted(self.__dict__)
            if isinstance(getattr(self, n), Parameter)
                and (all or getattr(self, n).estimate)
        )
        
    def param_change_events(self, enabled):
        for n, p in self.get_params(all=True):
            p.fire_change_events = enabled
    
    @property
    def spacecraft_pos(self):
        return self.x_off.value, self.y_off.value, self.z_off.value

    @spacecraft_pos.setter
    def spacecraft_pos(self, pos):
        self.z_off.value = pos[2]

        half_range = abs(pos[2] / 170 * 4)
        self.x_off.range = (pos[0] - half_range, pos[0] + half_range)
        self.x_off.value = pos[0]

        self.y_off.range = (pos[1] - half_range, pos[1] + half_range)
        self.y_off.value = pos[1]

    @property
    def spacecraft_rot(self):
        return self.x_rot.value, self.y_rot.value, self.z_rot.value

    @spacecraft_rot.setter
    def spacecraft_rot(self, r):
        self.x_rot.value, self.y_rot.value, self.z_rot.value = r

    @property
    def asteroid_axis(self):
        return self.ast_x_rot.value, self.ast_y_rot.value, self.ast_z_rot.value
    
    @asteroid_axis.setter
    def asteroid_axis(self, r):
        self.ast_x_rot.value, self.ast_y_rot.value, self.ast_z_rot.value = r
        self.update_asteroid_model()

    @property
    def spacecraft_dist(self):
        return math.sqrt(sum(x**2 for x in self.spacecraft_pos))

    def asteroid_rotation_from_model(self):
        self.ast_x_rot.value = math.degrees(self.asteroid.axis_latitude)
        self.ast_y_rot.value = math.degrees(self.asteroid.axis_longitude)
        self.ast_z_rot.value = (math.degrees(self.asteroid.rotation_pm) + 180) % 360 - 180

    def update_asteroid_model(self):
        self.asteroid.axis_latitude = math.radians(self.ast_x_rot.value)
        self.asteroid.axis_longitude = math.radians(self.ast_y_rot.value)
        self.asteroid.rotation_pm = math.radians(self.ast_z_rot.value)

    @property
    def real_spacecraft_pos(self):
        return self.x_off.real_value, self.y_off.real_value, self.z_off.real_value
    
    @real_spacecraft_pos.setter
    def real_spacecraft_pos(self, rv):
        self.x_off.real_value, self.y_off.real_value, self.z_off.real_value = rv

    @property
    def real_spacecraft_rot(self):
        return self.x_rot.real_value, self.y_rot.real_value, self.z_rot.real_value
    
    @real_spacecraft_rot.setter
    def real_spacecraft_rot(self, rv):
        self.x_rot.real_value, self.y_rot.real_value, self.z_rot.real_value = rv

    @property
    def real_asteroid_axis(self):
        return self.ast_x_rot.real_value, self.ast_y_rot.real_value, self.ast_z_rot.real_value
    
    @real_asteroid_axis.setter
    def real_asteroid_axis(self, rv):
        self.ast_x_rot.real_value, self.ast_y_rot.real_value, self.ast_z_rot.real_value = rv


    def rotate_spacecraft(self, q):
        new_q = self.spacecraft_q() * q
        self.x_rot.value, self.y_rot.value, self.z_rot.value = \
            list(map(math.degrees, tools.q_to_ypr(new_q)))

    def rotate_asteroid(self, q):
        """ rotate asteroid in spacecraft frame """
        
        ast = self.asteroid
        sc2ast_q = SystemModel.frm_conv_q(SystemModel.SPACECRAFT_FRAME, SystemModel.ASTEROID_FRAME)

        # global rotation q on asteroid in sc frame, followed by local rotation to asteroid frame
        new_q = q * ast.rotation_q(self.time.value) * sc2ast_q
        ast.axis_latitude, ast.axis_longitude, new_theta = tools.q_to_ypr(new_q)
        
        old_theta = ast.rotation_theta(self.time.value)
        ast.rotation_pm = tools.wrap_rads(ast.rotation_pm + new_theta - old_theta)
        
        self.asteroid_rotation_from_model()

    def spacecraft_q(self):
        return tools.ypr_to_q(*list(map(
                math.radians,
                (self.x_rot.value, self.y_rot.value, self.z_rot.value)
        )))
        
    def real_spacecraft_q(self):
        return tools.ypr_to_q(*list(map(
                math.radians,
                (self.x_rot.real_value, self.y_rot.real_value, self.z_rot.real_value)
        )))
        
    def asteroid_q(self):
        return self.asteroid.rotation_q(self.time.value)
    
    def real_asteroid_q(self):
        org_ast_axis = self.asteroid_axis
        self.asteroid_axis = self.real_asteroid_axis
        
        q = self.asteroid.rotation_q(self.time.real_value)
        
        self.asteroid_axis = org_ast_axis
        return q
    
        
    def gl_sc_asteroid_rel_q(self, discretize_tol=False):
        """ rotation of asteroid relative to spacecraft in opengl coords """
        self.update_asteroid_model()
        sc_ast_rel_q = self.sc_asteroid_rel_q() # why cant have: * SystemModel.sc2gl_q ??

        if discretize_tol:
            qq = tools.discretize_q(sc_ast_rel_q, discretize_tol)
            err_q = sc_ast_rel_q*qq.conj()
            sc_ast_rel_q = qq
        
        sc_ast_rel_q = SystemModel.sc2gl_q.conj()*sc_ast_rel_q
        if not BATCH_MODE and DEBUG:
            print('asteroid x-axis: %s'%tools.q_times_v(sc_ast_rel_q, np.array([1, 0, 0])))
        
        return sc_ast_rel_q, err_q if discretize_tol else False
    
    
    def sc_asteroid_rel_q(self, time=None):
        """ rotation of asteroid relative to spacecraft in spacecraft coords """
        ast_q = self.asteroid.rotation_q(time or self.time.value)
        sc_q = self.spacecraft_q()
        return sc_q.conj() * ast_q


    def real_sc_asteroid_rel_q(self):
        org_sc_rot = self.spacecraft_rot
        org_ast_axis = self.asteroid_axis
        self.spacecraft_rot = self.real_spacecraft_rot
        self.asteroid_axis = self.real_asteroid_axis
        
        q_tot = self.sc_asteroid_rel_q(time=self.time.real_value)
        
        self.spacecraft_rot = org_sc_rot
        self.asteroid_axis = org_ast_axis
        return q_tot
    
    
    def swap_values_with_real_vals(self):
        for n, p in self.get_params(True):
            assert p.real_value is not None, 'real value missing for %s'%n
            assert p.value is not None, 'current value missing %s'%n
            tmp = p.value
            p.value = p.real_value
            p.real_value = tmp
    
    def calc_shift_err(self):
        est_vertices = self.sc_asteroid_vertices()
        self.swap_values_with_real_vals()
        target_vertices = self.sc_asteroid_vertices()
        self.swap_values_with_real_vals()
        return tools.sc_asteroid_max_shift_error(est_vertices, target_vertices)
    
    
    def sc_asteroid_vertices(self, real=False):
        """ asteroid vertices rotated and translated to spacecraft frame """
        if self.real_shape_model is None:
            return None

        sc_ast_q = self.real_sc_asteroid_rel_q() if real else self.sc_asteroid_rel_q()
        sc_pos = self.real_spacecraft_pos if real else self.spacecraft_pos
        
        return tools.q_times_mx(sc_ast_q, np.array(self.real_shape_model.vertices)) \
                + tools.q_times_v(SystemModel.sc2gl_q, sc_pos)
    
    def light_rel_dir(self, err_q=False):
        """ direction of light relative to spacecraft in opengl coords """
        ast_v = tools.normalize_v(self.asteroid.position(self.time.value))
        sc_q = self.spacecraft_q()
        err_q = (err_q or np.quaternion(1,0,0,0))
        return tools.q_times_v(SystemModel.sc2gl_q.conj() * err_q.conj() * sc_q.conj(), ast_v)
        
    def solar_elongation(self, real=False):
        ast_v = self.asteroid.position(self.time.real_value if real else self.time.value)
        sc_q = self.real_spacecraft_q() if real else self.spacecraft_q()
        elong, direc = tools.solar_elongation(ast_v, sc_q)
        if not BATCH_MODE and DEBUG:
            print('elong: %.3f | dir: %.3f' % (
                math.degrees(elong), math.degrees(direc)))
        return elong, direc
    
    
    def rel_rot_err(self):
        return tools.angle_between_q(
            self.sc_asteroid_rel_q(),
            self.real_sc_asteroid_rel_q())
    
    def lat_pos_err(self):
        real_pos = self.real_spacecraft_pos
        err = np.subtract(self.spacecraft_pos, real_pos)
        return math.sqrt(err[0]**2 + err[1]**2) / abs(real_pos[2])

    def dist_pos_err(self):
        real_d = self.real_spacecraft_pos[2]
        return abs(self.spacecraft_pos[2] - real_d) / abs(real_d)
        
    def save_state(self, filename, printout=False):
        config = configparser.ConfigParser()
        filename = filename+('.lbl' if len(filename)<5 or filename[-4:]!='.lbl' else '')
        config.read(filename)
        config.add_section('main')
        config.add_section('real')
        
        for n, p in self.get_params(all=True):
            config.set('main', n, str(p.value))
            if p.real_value is not None:
                config.set('real', n, str(p.real_value))

        if self.asteroid.real_position is not None:
            config.set('real', 'sun_asteroid_pos', str(self.asteroid.real_position))

        if not printout:
            with open(filename, 'w') as f:
                config.write(f)
        else:
            config.write(sys.stdout)
    
    def load_state(self, filename, sc_ast_vertices=False):
        if not os.path.isfile(filename):
            raise FileNotFoundError(filename)
        
        config = configparser.ConfigParser()
        filename = filename+('.lbl' if len(filename)<5 or filename[-4:]!='.lbl' else '')
        config.read(filename)
        
        for n, p in self.get_params(all=True):
            v = float(config.get('main', n))
            if n == 'time':
                rp = self.asteroid.rotation_period
                p.range = (v-rp/2, v+rp/2)
            p.value = v
            
            rv = config.get('real', n, fallback=None)
            if rv is not None:
                p.real_value = float(rv)
        
        rv = config.get('real', 'sun_asteroid_pos', fallback=None)
        if rv is not None:
            self.asteroid.real_position = np.fromstring(rv[1:-1], dtype=np.float, sep=' ')
        
        assert np.isclose(self.time.value, float(config.get('main', 'time'))), \
               'Failed to set time value: %s vs %s'%(self.time.value, float(config.get('main', 'time')))
               
        self.update_asteroid_model()
        
        if sc_ast_vertices:
            # get real relative position of asteroid model vertices
            self.real_sc_ast_vertices = self.sc_asteroid_vertices(real=True)
    
    @staticmethod
    def frm_conv_q(fsrc, fdst):
        fqm = {
            SystemModel.OPENGL_FRAME:np.quaternion(1,0,0,0),
            SystemModel.OPENCV_FRAME:SystemModel.cv2gl_q,
            SystemModel.SPACECRAFT_FRAME:SystemModel.sc2gl_q,
            SystemModel.ASTEROID_FRAME:SystemModel.ast2gl_q,
        }
        return fqm[fsrc]*fqm[fdst].conj()

    
    def __repr__(self):
        return (
              'system state:\n\t%s\n'
            + '\nsolar elongation: %s\n'
            + '\nasteroid rotation: %.2f\n'
        ) % (
            '\n\t'.join('%s = %s'%(n, p) for n, p in self.get_params(all=True)), 
            tuple(map(math.degrees, self.solar_elongation())),
            math.degrees(self.asteroid.rotation_theta(self.time.value)),
        )
        

class Asteroid():
    def __init__(self, *args, **kwargs):
        self.name = '67P/Churyumov-Gerasimenko'
        
        self.real_position = None
        
        # for cross section, assume spherical object and 2km radius
        self.mean_cross_section = math.pi*2000**2
        
        # epoch for orbital elements, 2010-Oct-22.0 TDB
        self.oe_epoch = Time(2455491.5, format='jd')

        # orbital elements (from https://ssd.jpl.nasa.gov/sbdb.cgi)
        # reference: JPL K154/1 (heliocentric ecliptic J2000)
        self.eccentricity = .6405823233437267
        self.semimajor_axis = 3.464737502510219 * const.au
        self.inclination = math.radians(7.043680712713979)
        self.longitude_of_ascending_node = math.radians(50.18004588418096)
        self.argument_of_periapsis = math.radians(12.69446409956478)
        self.mean_anomaly = math.radians(91.76808585530111)

        #other
        self.aphelion = 5.684187101644357 * const.au
        self.perihelion = 1.245287903376082 * const.au
        self.orbital_period = 2355.612944885578*24*3600 # seconds
        #self.true_anomaly = math.radians(145.5260853202137 ??)
  
        # rotation period
        # from http://www.aanda.org/articles/aa/full_html/2015/11/aa26349-15/aa26349-15.html
        #   - 12.4043h (2014 aug-oct)
        # from http://www.sciencedirect.com/science/article/pii/S0019103516301385?via%3Dihub
        #   - 12.4304h (19 May 2015)
        #   - 12.305h (10 Aug 2015)
        self.rot_epoch = Time('J2000')

        #self.rotation_velocity = 2*math.pi/12.4043/3600 # prograde, in rad/s
        # --- above seems incorrect based on the pics, own estimate
        # based on ROS_CAM1_20150720T165249 - ROS_CAM1_20150721T075733
        if False:
            self.rotation_velocity = 2*math.pi/12.4043/3600
        else:
            # 2014-08-01 - 2014-09-02: 0.4/25
            self.rotation_velocity = 2*math.pi/12.4043/3600 -math.radians(0.4/25)/24/3600  #0.3754
       
        # for rotation phase shift, will use as equatorial longitude of
        #   asteroid zero longitude (cheops) at J2000, based on 20150720T165249
        #   papar had 114deg in it
        # for precession cone center (J2000), paper had 69.54, 64.11
        if False:
            tlat, tlon, tpm = 69.54, 64.11, 114
        else:
            # pm: 2014-08-01 - 2014-09-02: -9; 2015-06-13: -143
            tlat, tlon, tpm = 64.11, 69.54, -9
        
        self.rotation_pm = math.radians(tpm)
        self.axis_latitude, self.axis_longitude = \
                (math.radians(tlat), math.radians(tlon)) if USE_ICRS else \
                tools.equatorial_to_ecliptic(tlat*units.deg, tlon*units.deg)
        
        self.precession_cone_radius = math.radians(0.14)    # other paper 0.15+-0.03 deg
        self.precession_period = 10.7*24*3600  # other paper had 11.5+-0.5 days
        self.precession_pm = math.radians(0.288)
        
    @property
    def rotation_period(self):
        return 2*math.pi/self.rotation_velocity
    
    def rotation_theta(self, timestamp):
        dt = (Time(timestamp, format='unix') - self.rot_epoch).sec
        theta = (self.rotation_pm + self.rotation_velocity*dt) % (2*math.pi)
        return theta
        
    def rotation_q(self, timestamp):
        theta = self.rotation_theta(timestamp)
        
        # TODO: use precession info
        
        # orient z axis correctly, rotate around it
        ast2sc_q = SystemModel.frm_conv_q(SystemModel.ASTEROID_FRAME, SystemModel.SPACECRAFT_FRAME)
        return tools.ypr_to_q(self.axis_latitude, self.axis_longitude, theta) \
                * ast2sc_q
    
    def position(self, timestamp):
        if self.real_position is not None:
            return self.real_position
        
        # from http://space.stackexchange.com/questions/8911/determining-\
        #                           orbital-position-at-a-future-point-in-time
        
        # convert unix seconds to seconds since oe_epoch
        dt = (Time(timestamp, format='unix') - self.oe_epoch).sec
        
        # mean anomaly M
        M = (self.mean_anomaly + 2*math.pi*dt/self.orbital_period) % (2*math.pi)
        
        # eccentric anomaly E, orbit plane coordinates P & Q
        ecc = self.eccentricity
        E = tools.eccentric_anomaly(ecc, M)
        P = self.semimajor_axis * (math.cos(E) - ecc)
        Q = self.semimajor_axis * math.sin(E) * math.sqrt(1 - ecc**2)
        
        # rotate by argument of periapsis
        w = self.argument_of_periapsis
        x = math.cos(w) * P - math.sin(w) * Q
        y = math.sin(w) * P + math.cos(w) * Q
        
        # rotate by inclination
        z = math.sin(self.inclination) * x
        x = math.cos(self.inclination) * x
        
        # rotate by longitude of ascending node
        W = self.longitude_of_ascending_node
        xtemp = x
        x = math.cos(W) * xtemp - math.sin(W) * y
        y = math.sin(W) * xtemp + math.cos(W) * y
        
        # corrections for ROS_CAM1_20150720T113057
        if(False):
            x += 1.5e9*units.m
            y += -1e9*units.m
            z += -26.55e9*units.m
        
        v_ba = np.array([x.value, y.value, z.value])
        if not USE_ICRS:
            sc = SkyCoord(x=x, y=y, z=z, frame='icrs',
                          representation='cartesian', obstime='J2000')\
                .transform_to('heliocentrictrueecliptic')\
                .represent_as('cartesian')
            v_ba = np.array([sc.x.value, sc.y.value, sc.z.value])
        
        return v_ba
    