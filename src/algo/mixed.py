import math

import numpy as np
import quaternion
import cv2

from algo.base import AlgorithmBase
from settings import *
from algo import tools
#from algo.centroid import CentroidAlgo
#from algo.keypoint import KeypointAlgo
from algo.tools import PositioningException


class MixedAlgo(AlgorithmBase):
    def __init__(self, centroid, keypoint, **kwargs):
        super(MixedAlgo, self).__init__(centroid.system_model, centroid.render_engine, centroid.obj_idx)
        self._centroid = centroid
        self._keypoint = keypoint

    def run(self, sce_img, outfile, **kwargs):
        centroid_result = None
        try:
            if True:
                self._centroid.adjust_iteratively(sce_img, None, **kwargs)
                sc_r = self.system_model.spacecraft_rot
                centroid_result = self.system_model.spacecraft_pos
            else:
                centroid_result = self.system_model.real_spacecraft_pos
            #kwargs['init_z'] = centroid_result[2]

            x_off, y_off = tools.calc_img_xy(*centroid_result)
            kwargs['match_mask_params'] = (x_off-CAMERA_WIDTH/2, y_off-CAMERA_HEIGHT/2, centroid_result[2])

        except PositioningException as e:
            if str(e) == 'No asteroid found':
                raise e
            elif not 'Asteroid too close' in str(e) and DEBUG:
                print('Centroid algo failed with: %s'%(e,))

        try:
            self._keypoint.solve_pnp(sce_img, outfile, **kwargs)
            ok = True
        except PositioningException as e:
            if centroid_result and kwargs.get('centroid_fallback', False) and centroid_result[2] < -MIN_MED_DISTANCE:
                self.system_model.spacecraft_rot = sc_r
                self.system_model.spacecraft_pos = centroid_result
                if DEBUG:
                    print('Using centroid result as keypoint algo failed: %s'%(e,))
            else:
                raise e