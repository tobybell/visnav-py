# Based on DT-SLAM article:
#  - "DT-SLAM: Deferred Triangulation for Robust SLAM", Herrera, Kim, Kannala, Pulli, Heikkila
#    https://ieeexplore.ieee.org/stamp/stamp.jsp?tp=&arnumber=7035876
#
# code also available, it wasn't used for any reference though:
#  - https://github.com/plumonito/dtslam/tree/master/code/dtslam
#
# Note: check ATON (DLR project) for references of using 3d-3d correspondences for absolute navigation

import copy

import math

import numpy as np
import quaternion
import cv2

from algo import tools
from algo.bundleadj import bundle_adj
from algo.model import SystemModel, Asteroid
from iotools.objloader import ShapeModel


class Pose:
    def __init__(self, loc, quat: quaternion, loc_s2, so3_s2):
        self.loc = np.array(loc)
        self.quat = quat
        self.loc_s2 = np.array(loc_s2)
        self.so3_s2 = np.array(so3_s2)

    def __add__(self, dpose):
        assert isinstance(dpose, DeltaPose), 'Can only add DeltaPose to this'
        return Pose(
            self.loc + dpose.loc,
            dpose.quat * self.quat,
            self.loc_s2 + dpose.loc_s2,
            self.so3_s2 + dpose.so3_s2
        )

    def __sub__(self, pose):
        return DeltaPose(
            self.loc - pose.loc,
            pose.quat.conj() * self.quat,
            self.loc_s2 - pose.loc_s2,
            self.so3_s2 - pose.so3_s2
        )


class DeltaPose(Pose):
    pass


class PoseEstimate:
    def __init__(self, prior: Pose, post: Pose):
        self.prior = prior
        self.post = post


class Frame:
    _NEXT_ID = 1

    def __init__(self, time, image, img_sc, pose: PoseEstimate, kps_uv: dict=None, id=None):
        self._id = Frame._NEXT_ID if id is None else id
        Frame._NEXT_ID = max(self._id + 1, Frame._NEXT_ID)
        self.time = time
        self.image = image
        self.img_sc = img_sc
        self.pose = pose
        self.kps_uv = kps_uv or {}   # dict of keypoints with keypoint img coords in this frame
        self.ini_kp_count = len(self.kps_uv)

    @property
    def id(self):
        return self._id

    def __hash__(self):
        return self._id


class Keypoint:
    _NEXT_ID = 1

    def __init__(self, id=None):
        self._id = Keypoint._NEXT_ID if id is None else id
        Keypoint._NEXT_ID = max(self._id + 1, Keypoint._NEXT_ID)
        # self.frames_pts = frames_pts  # dict of frames with keypoint img coords in each frame
        self.pt3d = None
        self.total_count = 0
        self.inlier_count = 0
        self.inlier_time = None

    @property
    def id(self):
        return self._id

    def __hash__(self):
        return self._id


class State:
    def __init__(self):
        self.initialized = False
        self.keyframes = []
        self.map2d = {}  # id => Keypoint, all keypoints with only uv coordinates (still no 3d coords)
        self.map3d = {}
        self.last_frame = None
        self.last_success_time = None


class VisualOdometry:
    # visual odometry/slam parts:
    #  - feature keypoint detector
    #  - feature matcher
    #  - pose estimator
    #  - triangulator
    #  - keyframe acceptance decider
    #  - keyframe addition logic
    #  - map maintainer (remove and/or adjust keyframes and/or features)
    #  - loop closer or absolute pose estimator

    (
        KEYPOINT_SHI_TOMASI,    # exception thrown if MATCH_DESCRIPTOR used
        KEYPOINT_FAST,          # ORB descriptor if MATCH_DESCRIPTOR used
        KEYPOINT_AKAZE,         # AKAZE descriptor if MATCH_DESCRIPTOR used
    ) = range(3)
    DEF_KEYPOINT_ALGO = KEYPOINT_FAST
    DEF_MIN_KEYPOINT_DIST = 7

    (
        MATCH_FLOW,
        MATCH_DESCRIPTOR,
    ) = range(2)
    DEF_FEATURE_MATCHING = MATCH_FLOW

    (
        POSE_RANSAC_2D,         # always use essential matrix only
        POSE_RANSAC_3D,         # solve pnp when enough 3d points available in map, fallback to essential matrix if fails
        POSE_RANSAC_MIXED,      # solve pose from both 2d-2d and 3d-2d matches using ransac, optimize common cost function using inliers only
    ) = range(3)
    DEF_POSE_ESTIMATION = POSE_RANSAC_MIXED

    # keyframe addition
    DEF_NEW_KF_MIN_INLIER_RATIO = 0.7              # remaining inliers from previous keyframe features
    DEF_NEW_KF_MIN_DISPL_FOV_RATIO = 0.005         # displacement relative to the fov for triangulation
    DEF_NEW_KF_TRIANGULATION_TRIGGER_RATIO = 0.3   # ratio of 2d points tracked that can be triangulated
    DEF_NEW_KF_TRANS_KP3D_ANGLE = math.radians(5)  # change in viewpoint relative to a 3d point
    DEF_NEW_KF_TRANS_KP3D_RATIO = 0.1              # ratio of 3d points with significant viewpoint change
    DEF_NEW_KF_ROT_ANGLE = math.radians(10)        # new keyframe if orientation changed by this much

    # map maintenance
    DEF_MAX_KEYFRAMES = 7
    DEF_MAX_MARG_RATIO = 0.90
    DEF_SEPARATE_BA_THREAD = False
    DEF_REMOVAL_USAGE_LIMIT = 5        # 3d keypoint valid for removal if it was available for use this many times
    DEF_REMOVAL_RATIO = 0.2            # 3d keypoint inlier participation ratio below which the keypoint is discarded
    DEF_REMOVAL_AGE = 4                # remove if last inlier was this many keyframes ago

    DEF_UNCERTAINTY_LIMIT_RATIO = 30   # current solution has to be this much more uncertain as new initial pose to change base pose
    DEF_MIN_INLIERS = 20
    DEF_RESET_TIMEOUT = 30*60          # reinitialize if this many seconds without successful pose estimate
    DEF_MIN_FEATURE_INTENSITY = 15     # minimum level of intensity required near a keypoint

    DEF_USE_BA = True
    DEF_MAX_BA_KEYFRAMES = 7
    DEF_BA_INTERVAL = 3                # run ba every this many keyframes

    # TODO: (3) try out matching triangulated 3d points to 3d model using icp for all methods with 3d points
    # TODO: (3) try to init pose globally (before ICP) based on some 3d point-cloud descriptor
    DEF_USE_ICP = False

    # TODO: (3) get better distributed keypoints by using grids
    # TODO: (1) read dt-slam paper

    def __init__(self, sm, img_width=None, verbose=0, pause=0, **kwargs):
        self.sm = sm
        self.img_width = img_width
        self.verbose = verbose
        self.pause = pause
        self.cam_mx = self.sm.cam.intrinsic_camera_mx()

        # set params
        for attr in dir(VisualOdometry):
            if attr[:4] == 'DEF_':
                key = attr[4:].lower()
                setattr(self, key, kwargs.get(key, getattr(VisualOdometry, attr)))

        self.lk_params = {
            'winSize': (32, 32),
            'maxLevel': 4,
            'criteria': (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 10, 0.05),
        }
        if self.keypoint_algo == VisualOdometry.KEYPOINT_SHI_TOMASI:
            self.kp_params = {
                'maxCorners': 2000,
                'qualityLevel': 0.05,  # default around 0.05?
                'minDistance': self.min_keypoint_dist,
                'blockSize': 7,
            }
        elif self.keypoint_algo == VisualOdometry.KEYPOINT_FAST:
            self.kp_params = {
                'threshold': 10,         # default around 25?
                'nonmaxSuppression': True,
            }
        elif self.keypoint_algo == VisualOdometry.KEYPOINT_AKAZE:
            self.kp_params = {
                'diffusivity': cv2.KAZE_DIFF_CHARBONNIER, # default: cv2.KAZE_DIFF_PM_G2, KAZE_DIFF_CHARBONNIER
                'threshold': 0.001,          # default: 0.001
                'nOctaves': 4,               # default: 4
                'nOctaveLayers': 4,          # default: 4
            }

        # state
        self.state = State()

        # current frame specific temp value cache
        self.cache = {}
        self._track_image = None    # for debug purposes
        self._track_colors = None   # for debug purposes

    def __reduce__(self):
        return (VisualOdometry, (
            # init params, state
        ))

    def process(self, new_img, new_time, prior_pose: Pose) -> PoseEstimate:
        # reset cache
        self.cache.clear()

        # initialize new frame
        new_frame = self.initialize_frame(new_time, new_img, prior_pose)

        # maybe initialize state
        if not self.state.initialized:
            self.initialize(new_frame)
            return None

        # track/match keypoints
        self.track_keypoints(new_frame)

        # estimate pose
        self.estimate_pose(new_frame)

        # maybe do failure recovery
        if new_frame.pose.post is None:
            # TODO: (3) Implement failure recovery
            # if fail for too long, reinitialize (excl if only one keyframe)
            dt = (new_frame.time - self.state.last_success_time).total_seconds()
            if len(self.state.keyframes) > 1 and dt > self.reset_timeout:
                self.state.initialized = False
            # if too few keypoints to track, reinitialize
            if len(new_frame.kps_uv) < 2*self.min_inliers:
                self.state.initialized = False

        # add new frame as keyframe?      # TODO: (3) run in another thread
        if self.is_new_keyframe(new_frame):
            self.add_new_keyframe(new_frame)

            # maybe do map maintenance    # TODO: (3) run in yet another thread
            if self.is_maintenance_time():
                self.maintain_map()

        self.state.last_frame = new_frame
        return copy.deepcopy(new_frame.pose.post)

    def initialize_frame(self, time, image, prior_pose):
        if self.verbose:
            print('\nnew frame')

        # maybe scale image
        img_sc = 1
        if self.img_width is not None:
            img_sc = self.img_width / image.shape[1]
            image = cv2.resize(image, None, fx=img_sc, fy=img_sc)

        return Frame(time, image, img_sc, PoseEstimate(prior=prior_pose, post=None))

    def initialize(self, new_frame):
        if self.verbose:
            print('initializing tracking')
        self.state = State()
        new_frame.pose.post = new_frame.pose.prior
        self.add_new_keyframe(new_frame)

        # check that init ok and enough features found
        if len(self.state.map2d) > self.DEF_MIN_INLIERS * 2:
            self.state.last_frame = new_frame
            self.state.last_success_time = new_frame.time
            self.state.initialized = True

    def check_features(self, image, in_kp2d, simple=False):
        mask = np.ones((len(in_kp2d),), dtype=np.bool)

        if not simple:
            out_kp2d = self._detect_features(image, in_kp2d, existing=True)
            for i, pt in enumerate(in_kp2d):
                d = np.min(np.max(np.abs(out_kp2d - pt), axis=1))
                if d > 1.5:
                    mask[i] = False
        else:
            a_mask = self._asteroid_mask(image)
            h, w = image.shape[:2]
            for i, pt in enumerate(in_kp2d):
                d = 2
                y0, y1 = max(0, int(pt[0, 1]) - d), min(h, int(pt[0, 1]) + d)
                x0, x1 = max(0, int(pt[0, 0]) - d), min(w, int(pt[0, 0]) + d)
                if x1 <= x0 or y1 <= y0 \
                        or np.max(image[y0:y1, x0:x1]) < self.min_feature_intensity\
                        or a_mask[min(int(pt[0, 1]), h), min(int(pt[0, 0]), w)] == 0:
                    mask[i] = False

        return mask

    def detect_features(self, new_frame):
        kp2d = self._detect_features(new_frame.image, new_frame.kps_uv.values())

        for pt in kp2d:
            kp = Keypoint()  # Keypoint({new_frame.id: pt})
            new_frame.kps_uv[kp.id] = pt
            self.state.map2d[kp.id] = kp
        new_frame.ini_kp_count = len(new_frame.kps_uv)

        if self.verbose:
            print('%d new keypoints detected' % len(kp2d))

    def _asteroid_mask(self, image):
        _, mask = cv2.threshold(image, self.min_feature_intensity, 255, cv2.THRESH_BINARY)
        kernel = np.array([
                [0, 1, 1, 1, 0],
                [1, 1, 1, 1, 1],
                [1, 1, 1, 1, 1],
                [1, 1, 1, 1, 1],
                [0, 1, 1, 1, 0]], np.uint8)

        # exclude asteroid limb from feature detection
        # TODO: (2) optimize amount of dilation and erosion
        mask = cv2.dilate(mask, kernel, iterations=3)
        mask = cv2.erode(mask, kernel, iterations=6)

#        cv2.imshow('mask', mask)
 #       cv2.waitKey(15)

        return mask

    def _detect_features(self, image, kp2d, existing=False):
        # create mask defining where detection is to be done
        mask = self._asteroid_mask(image)
        if existing:
            mask_a = mask
            mask = np.zeros(image.shape, dtype=np.uint8)

        d, (h, w) = self.min_keypoint_dist, mask.shape
        for uv in kp2d:
            if existing:
                y, x = int(uv[0, 1]), int(uv[0, 0])
                mask[y:min(h, y+1), x:min(w, x+1)] = mask_a[y:min(h, y+1), x:min(w, x+1)]
            else:
                y0, y1 = max(0, int(uv[0, 1]) - d), min(h, int(uv[0, 1]) + d)
                x0, x1 = max(0, int(uv[0, 0]) - d), min(w, int(uv[0, 0]) + d)
                if x1 > x0 and y1 > y0:
                    mask[y0:y1, x0:x1] = 0

        if self.keypoint_algo == VisualOdometry.KEYPOINT_SHI_TOMASI:
            # detect Shi-Tomasi keypoints
            kp2d = cv2.goodFeaturesToTrack(image, mask=mask, **self.kp_params)
        elif self.keypoint_algo == VisualOdometry.KEYPOINT_FAST:
            # detect FAST keypoints
            det = cv2.FastFeatureDetector_create(**self.kp_params)
            kp2d = det.detect(image, mask=mask)
            kp2d = self.kp2arr(kp2d)
        elif self.keypoint_algo == VisualOdometry.KEYPOINT_AKAZE:
            # detect AKAZE keypoints
            det = cv2.AKAZE_create(**self.kp_params)
            kp2d = det.detect(image, mask=mask)
            kp2d = self.kp2arr(kp2d)
        else:
            assert False, 'unknown keypoint detection algorithm: %s' % self.feat

        return kp2d

    def track_keypoints(self, new_frame):
        lf, nf = self.state.last_frame, new_frame

        if len(lf.kps_uv) > 0:
            if self.feature_matching == VisualOdometry.MATCH_FLOW:
                # track keypoints using Lukas-Kanade method
                ids, old_kp2d = list(map(np.array, zip(*lf.kps_uv.items()) if len(lf.kps_uv) > 0 else ([], [])))
                new_kp2d, mask, err = cv2.calcOpticalFlowPyrLK(lf.image, nf.image, old_kp2d, None, **self.lk_params)

                # extra sanity check on tracked points, set mask to false if keypoint quality too poor
                mask2 = self.check_features(nf.image, new_kp2d, simple=True)

                mask = np.logical_and(mask.astype(np.bool).flatten(), mask2)
                new_kp2d = new_kp2d[mask, :, :]

                # delete non-tracked keypoints!
                # TODO: (3) dont delete 3d-keypoints, select visible, project and find them in new image
                for id in ids[np.logical_not(mask)]:
                    self.del_keypoint(id)

                ids = ids[mask]

            elif self.feature_matching == VisualOdometry.MATCH_DESCRIPTOR:
                # track keypoints by matching descriptors, possibly searching only nearby areas
                assert False, 'not implemented'

            else:
                assert False, 'invalid feature matching method'

            nf.kps_uv = {id: uv for id, uv in zip(ids, new_kp2d)}
            nf.ini_kp_count = len(nf.kps_uv)
            if self.verbose:
                print('Tracking: %d/%d' % (len(new_kp2d), len(old_kp2d)))

    def estimate_pose(self, new_frame):
        rf, nf = self.state.keyframes[-1], new_frame
        dr, dq = None, None
        inliers = None

        if self.pose_estimation in (VisualOdometry.POSE_RANSAC_3D, VisualOdometry.POSE_RANSAC_MIXED):
            # solve pose using ransac & ap3p based on 3d-2d matches

            tmp = [(id, pt2d, self.state.map3d[id].pt3d)
                   for id, pt2d in nf.kps_uv.items()
                   if id in self.state.map3d]
            ids, pts2d, pts3d = list(map(np.array, zip(*tmp) if len(tmp) else ([], [], [])))

            ok = False
            if len(pts3d) > self.min_inliers:
                ok, rv, r, inliers = cv2.solvePnPRansac(pts3d, pts2d / nf.img_sc, self.cam_mx, None,
                                                         iterationsCount=10000, reprojectionError=8, flags=cv2.SOLVEPNP_AP3P)

            if ok and len(inliers) > self.min_inliers:
                if self.verbose:
                    print('PnP: %d/%d' % (len(inliers), len(pts2d)))
                q = tools.angleaxis_to_q(rv).conj()     # solvepnp returns orientation of system origin vs camera (!)
                r = -tools.q_times_v(q, r)              # solvepnp returns a vector from camera to system origin (!)

                # record keypoint stats
                for id in ids:
                    self.state.map3d[id].total_count += 1
                for i in inliers.flatten():
                    self.state.map3d[ids[i]].inlier_count += 1
                    self.state.map3d[ids[i]].inlier_time = nf.time

                # solvePnPRansac has some bug that apparently randomly gives 180deg wrong answer
                # if abs(tools.q_to_ypr(q)[2]) > math.pi * 0.9:
                #     if self.verbose:
                #         print('rotated pnp-ransac solution by 180deg around x-axis')
                #     q = q * tools.ypr_to_q(0, 0, math.pi)
                #     r = -r

                # calculate delta-q and delta-r
                dq = q * rf.pose.post.quat.conj()
                dr = tools.q_times_v(rf.pose.post.quat.conj(), r.flatten() - rf.pose.post.loc)

            elif inliers is None:
                if self.verbose:
                    print('Too few 3D points matched for reliable pose estimation')
            elif len(inliers) > 0:
                if self.verbose:
                    print('PnP was left with too few inliers')
            else:
                if self.verbose:
                    print('PnP Failed')

        if dr is None or self.pose_estimation in (VisualOdometry.POSE_RANSAC_2D, VisualOdometry.POSE_RANSAC_MIXED):
            # include all tracked keypoints, i.e. also 3d points
            scale = np.linalg.norm(rf.pose.post.loc - nf.pose.prior.loc)
            tmp = [(id, pt2d, rf.kps_uv[id])
                   for id, pt2d in nf.kps_uv.items()
                   if id in rf.kps_uv]
            ids, new_kp2d, old_kp2d = list(map(np.array, zip(*tmp) if len(tmp) > 0 else ([], [], [])))

            R = None
            mask = 0
            if len(old_kp2d) > self.min_inliers:
                # solve pose using ransac & 5-point algo
                E, mask2 = cv2.findEssentialMat(old_kp2d / rf.img_sc, new_kp2d / nf.img_sc, self.cam_mx,
                                                method=cv2.RANSAC, prob=0.999, threshold=1.0)
                if self.verbose:
                    print('E-mat: %d/%d' % (np.sum(mask2), len(old_kp2d)))

                if np.sum(mask2) > self.min_inliers:
                    _, R, ur, mask = cv2.recoverPose(E, old_kp2d / rf.img_sc, new_kp2d / nf.img_sc, self.cam_mx, mask=mask2.copy())
                    if self.verbose:
                        print('E=>R: %d/%d' % (np.sum(mask), np.sum(mask2)))

            # TODO: (3) implement pure rotation estimation as a fallback as E can't solve for that

            if R is not None and np.sum(mask) > self.min_inliers:
                # recoverPose returns transformation from new to old (!)
                dq_ = quaternion.from_rotation_matrix(R).conj()
                dr_ = -tools.q_times_v(dq_, scale * ur)
                if self.pose_estimation == VisualOdometry.POSE_RANSAC_MIXED and dq is not None:
                    # mix result from solve pnp and recoverPose
                    # TODO: (3) do it by optimizing a common cost function including only inliers from both algos
                    n2d, n3d = np.sum(mask), len(inliers)
                    w2dq = n2d/(n2d + n3d * 1.25)
                    dq = tools.mean_q([dq, dq_], [1 - w2dq, w2dq])
                    w2dr = n2d/(n2d + n3d * 12.5)
                    dr = (1 - w2dr) * dr + w2dr*dr_
                else:
                    dq = dq_
                    dr = dr_

        if self.verbose:
            # TODO: (1) print delta pose relative to previous frame
            # TODO: (1) print delta delta-pose => diff between (prior_t - prior_{t-1}) and (post_t - post_{t-1})
            # TODO: (1) add scale parameter and estimate it continuously
            if dr is not None and dq is not None:
                print('delta q: ' + ' '.join(['%.3fdeg' % math.degrees(a) for a in tools.q_to_ypr(dq)]))
                print('delta v: ' + ' '.join(['%.3fm' % a for a in dr * 1000]))
            self._draw_tracks(nf, pause=self.pause)

        if dr is None or dq is None:
            new_frame.pose.post = None
            return

        # TODO: (2) calculate uncertainty / error var
        d_r_s2 = np.array([1, 1, 1]) * 0.1
        d_so3_s2 = np.array([1, 1, 1]) * 0.01

        # update pose and uncertainty
        new_frame.pose.post = Pose(
            rf.pose.post.loc + tools.q_times_v(rf.pose.post.quat, dr),
            dq * rf.pose.post.quat,
            rf.pose.post.loc_s2 + tools.q_times_v(rf.pose.post.quat, d_r_s2),
            rf.pose.post.so3_s2 + tools.q_times_v(rf.pose.post.quat, d_so3_s2),
        )
        self.state.last_success_time = new_frame.time

    def is_new_keyframe(self, new_frame):
        # check if
        #   a) should detect new feats as old ones don't work,
        #   b) can triangulate many 2d points,
        #   c) viewpoint changed for many 3d points, or
        #   d) orientation changed significantly
        #   TODO: (3) e) phase angle (=> appearance) changed significantly

        rf, nf = self.state.keyframes[-1], new_frame

        # pose solution available
        if nf.pose.post is None:
            return False

        #   a) should detect new feats as old ones don't work
        if len(nf.kps_uv)/rf.ini_kp_count < self.new_kf_min_inlier_ratio:
            return True

        #   d) orientation changed significantly
        if tools.angle_between_q(nf.pose.post.quat, rf.pose.post.quat) > self.new_kf_rot_angle:
            return True

        #   b) can triangulate many 2d points
        if self.pose_estimation in (VisualOdometry.POSE_RANSAC_3D, VisualOdometry.POSE_RANSAC_MIXED) \
                and len(self.triangulation_kps(nf))/len(self.state.map2d) > self.new_kf_triangulation_trigger_ratio:
            return True

        #   c) viewpoint changed for many 3d points
        if self.use_ba \
                and len(self.state.map3d) > 0 \
                and len(self.viewpoint_changed_kps(nf))/len(self.state.map3d) > self.new_kf_trans_kp3d_ratio:
            return True

        return False

    def add_new_keyframe(self, new_frame):
        self.state.keyframes.append(new_frame)
        self.detect_features(new_frame)
        if self.pose_estimation in (VisualOdometry.POSE_RANSAC_3D, VisualOdometry.POSE_RANSAC_MIXED):
            self.triangulate(new_frame)
        # if self.use_ba and len(self.state.map3d) > 0:
        #     self.bundle_adjustment(max_keyframes=1)

    def triangulation_kps(self, new_frame):
        """
        return 2d keypoints that can be triangulated as they have more displacement than new_kf_min_displ_fov_ratio
        together with the corresponding reference frame
        """
        kpset = self.cache.get('triangulation_kps', None)
        if kpset is not None:
            return kpset

        # return dict(kp_id => ref_frame)
        kpset = {}
        for id, uv in new_frame.kps_uv.items():
            if id in self.state.map2d:
                tmp = [(i, np.linalg.norm(f.kps_uv[id]/f.img_sc - uv/new_frame.img_sc))
                       for i, f in enumerate(self.state.keyframes)
                       if id in f.kps_uv]
                f_idxs, d = zip(*tmp) if len(tmp) > 0 else ([], [])

                i = np.argmax(d)
                if d[i]/np.linalg.norm(np.array(new_frame.image.shape)/new_frame.img_sc) > self.new_kf_min_displ_fov_ratio:
                    kpset[id] = self.state.keyframes[f_idxs[i]]

        self.cache['triangulation_kps'] = kpset
        return kpset

    def viewpoint_changed_kps(self, new_frame):
        """
        return 3d keypoints that are viewed from a significantly different angle compared to previus keyframe,
        return values is a set of keypoint ids
        """
        kpset = self.cache.get('viewpoint_changed_kps', None)
        if kpset is not None:
            return kpset

        ref_frame = self.state.keyframes[-1]

        tmp = [(id, self.state.map3d[id].pt3d)
               for id, uv in new_frame.kps_uv.items()
               if id in self.state.map3d]
        ids, pts3d = list(map(np.array, zip(*tmp) if len(tmp) > 0 else ([], [])))
        #pts3d = np.array(pts3d).reshape(-1, 3)
        dloc = (new_frame.pose.post.loc - ref_frame.pose.post.loc).reshape(1, 3)
        da = tools.angle_between_rows(pts3d, pts3d + dloc)
        kpset = set(ids[da > self.new_kf_trans_kp3d_angle])

        self.cache['viewpoint_changed_kps'] = kpset
        return kpset

    def triangulate(self, new_frame):
        # triangulate matched 2d keypoints to get 3d keypoints
        tr_kps = self.triangulation_kps(new_frame)

        # need transformation from camera to world coordinate origin, not vice versa (!)
        T1 = np.hstack((quaternion.as_rotation_matrix(new_frame.pose.post.quat.conj()),
                        -tools.q_times_v(new_frame.pose.post.quat.conj(), new_frame.pose.post.loc).reshape((-1, 1))))

        for kp_id, ref_frame in tr_kps.items():
            T0 = np.hstack((quaternion.as_rotation_matrix(ref_frame.pose.post.quat.conj()),
                            -tools.q_times_v(ref_frame.pose.post.quat.conj(), ref_frame.pose.post.loc).reshape((-1, 1))))

            # TODO: (3) use multipoint triangulation instead
            uv0 = ref_frame.kps_uv[kp_id] / ref_frame.img_sc
            uv1 = new_frame.kps_uv[kp_id] / new_frame.img_sc

            kp4d = cv2.triangulatePoints(self.cam_mx.dot(T0), self.cam_mx.dot(T1), uv0.reshape((-1,1,2)), uv1.reshape((-1,1,2)))
            kp = self.state.map2d.pop(kp_id)
            kp.pt3d = kp4d.T[:, :3].flatten() / kp4d.T[:, 3].flatten()
            self.state.map3d[kp_id] = kp

        if self.verbose:
            print('%d keypoints triangulated' % len(tr_kps))

    def bundle_adjustment(self, max_keyframes=None):
        if self.verbose:
            print('starting bundle adjustment')

        max_keyframes = max_keyframes or len(self.state.keyframes)

        tmp = [
            (pt.id, pt.pt3d)
            for pt in self.state.map3d.values()
            if pt.inlier_count > 0  # only include if been an inlier
        ]
        if len(tmp) == 0:
            return
        ids, pts3d = zip(*tmp)
        idmap = dict(zip(ids, np.arange(len(ids))))

        poses_mx = np.array([
            np.hstack((
                tools.q_to_angleaxis(f.pose.post.quat.conj(), compact=True),            # flip to cam -> world
                -tools.q_times_v(f.pose.post.quat.conj(), f.pose.post.loc).flatten()
            ))
            for f in self.state.keyframes[-max_keyframes:]
        ])

        cam_idxs, pt3d_idxs, pts2d = list(map(np.array, zip(*[
            (i, idmap[id], uv.flatten())
            for i, f in enumerate(self.state.keyframes[-max_keyframes:])
                for id, uv in f.kps_uv.items() if id in idmap
        ])))

        poses, pts3d = bundle_adj(poses_mx, np.array(pts3d), pts2d, cam_idxs, pt3d_idxs, self.cam_mx, max_nfev=10)

        for i, p in enumerate(poses):
            f = self.state.keyframes[-max_keyframes:][i]
            f.pose.post.quat = tools.angleaxis_to_q(p[:3]).conj()           # flip back so that world -> cam
            f.pose.post.loc = -tools.q_times_v(f.pose.post.quat, p[3:])
        for i, pt3d in enumerate(pts3d):
            self.state.map3d[ids[i]].pt3d = pt3d.flatten()

        if self.verbose:
            print('bundle adjustment complete')

    def is_maintenance_time(self):
        return self.state.keyframes[-1].id % self.ba_interval == 0

    def maintain_map(self):
        if len(self.state.keyframes) > self.max_keyframes:
            self.prune_keyframes()

        if self.pose_estimation in (VisualOdometry.POSE_RANSAC_3D, VisualOdometry.POSE_RANSAC_MIXED):
            # Remove 3d keypoints from map.
            # No need to remove 2d keypoints here as they are removed
            # elsewhere if 1) tracking fails, or 2) triangulated into 3d points.
            self.prune_map3d()

        if self.use_ba and len(self.state.map3d) > 0 and len(self.state.keyframes) >= self.max_ba_keyframes:
            self.bundle_adjustment(max_keyframes=self.max_ba_keyframes)

    def prune_keyframes(self):
        rem_kfs = self.state.keyframes[:-self.max_keyframes]
        self.state.keyframes = self.state.keyframes[-self.max_keyframes:]
        # no need to remove 3d points as currently only those are retained that can be tracked
        # no need to transform poses or 3d points as basis is the absolute pose
        if self.verbose:
            print('%d keyframes dropped' % len(rem_kfs))

    def prune_map3d(self):
        rem = []
        lim, kfs = self.removal_age, self.state.keyframes
        for kp in self.state.map3d.values():
            if kp.total_count >= self.removal_usage_limit and (kp.inlier_count/kp.total_count <= self.removal_ratio \
                    or len(kfs) > lim and kp.inlier_time <= kfs[-lim].time):
                rem.append(kp.id)
        for id in rem:
            self.del_keypoint(id)
        if self.verbose:
            print('%d 3d keypoints discarded' % len(rem))

    def del_keypoint(self, id):
        self.state.map2d.pop(id, False)
        self.state.map3d.pop(id, False)
        for f in self.state.keyframes:
            f.kps_uv.pop(id, False)
            self.state.last_frame.kps_uv.pop(id, False)

    def arr2kp(self, arr, size=7):
        return [cv2.KeyPoint(p[0, 0], p[0, 1], size) for p in arr]

    def kp2arr(self, kp):
        return np.array([k.pt for k in kp], dtype='f4').reshape((-1, 1, 2))

    def get_3d_map_pts(self):
        return np.array([pt.pt3d for pt in self.state.map3d.values()]).reshape((-1, 3))

    @staticmethod
    def get_2d_pts(frame):
        return np.array([uv.flatten() for uv in frame.kps_uv.values()]).reshape((-1, 2))

    def _draw_tracks(self, new_frame, pause=True, label='tracks'):
        f0, f1 = self.state.last_frame, new_frame
        ids, uv1 = zip(*[(id, uv.flatten()) for id, uv in f1.kps_uv.items()]) if len(f1.kps_uv) > 0 else ([], [])
        uv0 = [f0.kps_uv[id].flatten() for id in ids]
        if self._track_image is None:
            self._track_image = np.zeros((*f1.image.shape, 3), dtype=np.uint8)
            self._track_colors = np.random.randint(0, 255, (100, 3))
        else:
            self._track_image = (self._track_image * 0.8).astype(np.uint8)
        img = cv2.cvtColor(f1.image, cv2.COLOR_GRAY2RGB)

        for id, (x0, y0), (x1, y1) in zip(ids, uv0, uv1):
            self._track_image = cv2.line(self._track_image, (x1, y1), (x0, y0), self._track_colors[id % 100].tolist(), 1)
            img = cv2.circle(img, (x1, y1), 5, self._track_colors[id % 100].tolist(), -1)
        img = cv2.add(img, self._track_image)
        cv2.imshow(label, img)
        cv2.waitKey(0 if pause else 25)

    def _draw_matches(self, img1, img2, kp1, kp2, mask, pause=True, label='matches'):
        idxs = np.array(list(range(len(kp1))))[mask]
        matches = [[cv2.DMatch(i, i, 0)] for i in idxs]
        draw_params = {
            #            matchColor: (88, 88, 88),
            'singlePointColor': (0, 0, 255),
            #            'flags': cv2.DRAW_MATCHES_FLAGS_DRAW_RICH_KEYPOINTS,
        }
        # scale images, show
        img_sc = 768/img1.shape[0]
        sc_img1 = cv2.cvtColor(cv2.resize(img1, None, fx=img_sc, fy=img_sc, interpolation=cv2.INTER_CUBIC), cv2.COLOR_GRAY2RGB)
        sc_img2 = cv2.cvtColor(cv2.resize(img2, None, fx=img_sc, fy=img_sc, interpolation=cv2.INTER_CUBIC), cv2.COLOR_GRAY2RGB)
        img3 = cv2.drawMatchesKnn(sc_img1, self.arr2kp(kp1*img_sc), sc_img2, self.arr2kp(kp2*img_sc), matches, None, **draw_params)
        cv2.imshow(label, img3)
        cv2.waitKey(0 if pause else 25)


if __name__ == '__main__':
    import math
    import cv2

    import matplotlib.pyplot as plt
    from mpl_toolkits.mplot3d import Axes3D  # noqa: F401 unused import

    from missions.didymos import DidymosSystemModel
    from render.render import RenderEngine
    from settings import *

    sm = DidymosSystemModel(use_narrow_cam=False, target_primary=False, hi_res_shape_model=False)
    re = RenderEngine(sm.cam.width, sm.cam.height, antialias_samples=0)
    re.set_frustum(sm.cam.x_fov, sm.cam.y_fov, 0.05, 2)
    ast_v = np.array([0, 0, -sm.min_med_distance * 1])
    #q = tools.angleaxis_to_q((math.radians(2), 0, 1, 0))
    q = tools.angleaxis_to_q((math.radians(1), 1, 0, 0))
    #q = tools.rand_q(math.radians(2))
    #lowq_obj = sm.asteroid.load_noisy_shape_model(Asteroid.SM_NOISE_HIGH)
    obj = sm.asteroid.real_shape_model
    obj_idx = re.load_object(obj)
    # obj_idx = re.load_object(sm.asteroid.hires_target_model_file)

    odo = VisualOdometry(sm, sm.cam.width, verbose=True, pause=False)

    for t in range(60):
        ast_q = q**t
        image = re.render(obj_idx, ast_v, ast_q, np.array([1, 0, 0])/math.sqrt(1), gamma=1.8, get_depth=False)
        image = cv2.cvtColor(image, cv2.COLOR_RGB2GRAY)

        n_ast_q = ast_q * tools.rand_q(math.radians(.3))
        #n_ast_q = ast_q
        cam_q = SystemModel.cv2gl_q * n_ast_q.conj() * SystemModel.cv2gl_q.conj()
        cam_v = tools.q_times_v(cam_q * SystemModel.cv2gl_q, -ast_v)
        prior = Pose(cam_v, cam_q, np.ones((3,))*0.1, np.ones((3,))*0.01)

        # estimate pose
        res = odo.process(image, t, prior)

        if res is not None:
            tq = res.quat * SystemModel.cv2gl_q
            est_q = tq.conj() * res.quat.conj() * tq
            err_q = ast_q.conj() * est_q
            err_angle = tools.angle_between_q(ast_q, est_q)
            est_v = -tools.q_times_v(tq.conj(), res.loc)
            err_v = est_v - ast_v

            #print('\n')
            # print('rea ypr: %s' % ' '.join('%.1fdeg' % math.degrees(a) for a in tools.q_to_ypr(cam_q)))
            # print('est ypr: %s' % ' '.join('%.1fdeg' % math.degrees(a) for a in tools.q_to_ypr(res_q)))
            print('rea ypr: %s' % ' '.join('%.1fdeg' % math.degrees(a) for a in tools.q_to_ypr(ast_q)))
            print('est ypr: %s' % ' '.join('%.1fdeg' % math.degrees(a) for a in tools.q_to_ypr(est_q)))
            # print('err ypr: %s' % ' '.join('%.2fdeg' % math.degrees(a) for a in tools.q_to_ypr(err_q)))
            print('err angle: %.2fdeg' % math.degrees(err_angle))
            # print('rea v: %s' % ' '.join('%.1fm' % a for a in cam_v*1000))
            # print('est v: %s' % ' '.join('%.1fm' % a for a in res_v*1000))
            print('rea v: %s' % ' '.join('%.1fm' % a for a in ast_v*1000))
            print('est v: %s' % ' '.join('%.1fm' % a for a in est_v*1000))
            # print('err v: %s' % ' '.join('%.2fm' % a for a in err_v*1000))
            print('err norm: %.2fm\n' % np.linalg.norm(err_v*1000))

            if False and len(odo.state.map3d) > 0:
                pts3d = tools.q_times_mx(SystemModel.cv2gl_q.conj(), odo.get_3d_map_pts())
                tools.plot_vectors(pts3d)
                errs = tools.point_cloud_vs_model_err(pts3d, obj)
                print('\n3d map err mean=%.3f, sd=%.3f, n=%d' % (
                    np.mean(errs),
                    np.std(errs),
                    len(odo.state.map3d),
                ))

            # print('\n')
        else:
            print('no solution\n')

        #cv2.imshow('image', image)
        #cv2.waitKey()
