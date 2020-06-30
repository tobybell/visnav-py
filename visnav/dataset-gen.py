import argparse
import csv
import sys
import math

import numpy as np

from visnav.batch1 import get_system_model
from visnav.iotools import cnn_export
from visnav.testloop import TestLoop
from visnav.settings import *


# TODO:
#    - Include image rendered at cropped pose (for autoencoder target)
#    - Save a config file about parameters used for dataset generation
#    - Support usage of real navcam images for generating a validation/testing dataset
#    (- Upload dataset to triton?)


def main():
    args = parse_arguments()

    sm = get_system_model(args.mission, hi_res_shape_model=1)
    file_prefix_mod = ''
    img_file_postfix = 'cm' if args.sm_noise > 0 else ''
    log_prefix = 'dsg-'+args.mission+'-'+args.id+'-'
    cache_path = os.path.join(args.cache, args.mission, args.id)

    # operational zone only
    sm.min_distance = sm.min_med_distance
    sm.max_distance = sm.max_med_distance
    sm.min_elong = 180 - 100    # max phase angle = 100 deg

    print('starting to generate images')
    tl = TestLoop(sm, file_prefix_mod=file_prefix_mod, est_real_ast_orient=False,
                  state_generator=None, uniform_distance_gen=False, operation_zone_only=True,
                  cache_path=cache_path,
                  sm_noise=0, sm_noise_len_sc=SHAPE_MODEL_NOISE_LEN_SC,
                  navcam_cache_id=img_file_postfix, save_depth=True,
                  real_sm_noise=args.sm_noise, real_sm_noise_len_sc=args.sm_noise_len_sc,
                  real_tx_noise=args.tx_noise, real_tx_noise_len_sc=args.tx_noise_len_sc,
                  haze=args.haze,
                  jets=args.jets, jet_int_mode=args.jet_int_mode, jet_int_conc=args.jet_int_conc,
                  hapke_noise=args.hapke_noise,
                  hapke_th_sd=args.hapke_th_sd, hapke_w_sd=args.hapke_w_sd,
                  hapke_b_sd=args.hapke_b_sd, hapke_c_sd=args.hapke_c_sd,
                  hapke_shoe=args.hapke_shoe, hapke_shoe_w=args.hapke_shoe_w,
                  hapke_cboe=args.hapke_cboe, hapke_cboe_w=args.hapke_cboe_w)

    # check if can skip testloop iterations in case the execution died during previous execution
    log_entries = read_logfiles(log_prefix, args.max_rot_err)
    file_exists = [i for i, f in log_entries if os.path.exists(f) and f == tl.cache_file(i, postfix=img_file_postfix)+'.png']
    row_range = get_range(args.count, file_exists)

    if row_range is not None:
        tl.run(row_range, log_prefix=log_prefix,
               constant_sm_noise=True, smn_cache_id='lo',
               method='keypoint', feat=1, verbose=0)

    # export
    print('starting to export images')
    img_file_postfix = ('_' + img_file_postfix) if img_file_postfix else ''
    os.makedirs(args.output, exist_ok=True)  # make sure output folder exists

    # skip export of already existing images, process only images in given iteration range
    if ':' in args.count:
        start, end = map(int, args.count.split(':'))
    else:
        start, end = 0, int(args.count)
    imgfiles = read_logfiles(log_prefix, args.max_rot_err)
    imgfiles = [(f, os.path.join(args.output, os.path.basename(f))) for i, f in imgfiles if start <= i < end]
    ok_files = cnn_export.get_files_with_metadata(args.output)
    imgfiles = [sf for sf, df in imgfiles if not os.path.exists(df) or os.path.basename(df) not in ok_files]

    cnn_export.export(sm, args.output, src_imgs=imgfiles, trg_shape=(224, 224), img_postfix=img_file_postfix,
                      title="Synthetic Image Set, mission=%s, id=%s" % (args.mission, args.id), debug=0)


def sample_mosaic():
    import cv2

    args = parse_arguments()
    files = cnn_export.get_files_with_metadata(args.output)
    s = 56
    r, c = 6, 24
    comp = np.ones(((s+1)*r-1, (s+1)*c-1), dtype=np.uint8)*255
    for i, file in enumerate([f for f in files if f][0:r*c]):
        img = cv2.imread(os.path.join(args.output, file), cv2.IMREAD_GRAYSCALE)
        k, j = i // c, i % c
        comp[k*(s+1):(k+1)*(s+1)-1, j*(s+1):(j+1)*(s+1)-1] = cv2.resize(img, (s, s))
    cv2.imwrite('mosaic.png', comp)
    cv2.imshow('mosaic.png', comp)
    cv2.waitKey()


def get_range(org_range, exists):
    if ':' in org_range:
        start, end = map(int, org_range.split(':'))
    else:
        start, end = 0, int(org_range)
    if len(exists) > 0:
        start = max(start, np.max(exists)+1)
    if start >= end:
        return None
    return '%d:%d' % (start, end)


def read_logfiles(file_prefix, max_err):
    # find all files with given prefix, merge in order of ascending date
    logfiles = []
    for f in os.listdir(LOG_DIR):
        if f[:len(file_prefix)] == file_prefix and f[-4:] == '.log':
            fpath = os.path.join(LOG_DIR, f)
            logfiles.append((os.path.getmtime(fpath), fpath))
    logfiles = sorted(logfiles, key=lambda x: x[0])

    imgs, roterrs = {}, {}
    for _, logfile in logfiles:
        with open(logfile, newline='') as csvfile:
            data = csv.reader(csvfile, delimiter='\t')
            first = True
            for row in data:
                if len(row) > 10:
                    if first:
                        first = False
                        lbl_i = row.index('iter')
                        img_i = row.index('imgfile')
                        rot_i = row.index('rot error')
                    else:
                        try:
                            i = int(row[lbl_i])
                            imgs[i] = row[img_i]
                            roterrs[i] = float(row[rot_i])
                        except ValueError as e:
                            print('Can\'t convert roterr or iter on row %s' % (row[lbl_i],))
                            raise e

    images = [(i, imgs[i]) for i, e in roterrs.items() if not math.isnan(e) and e < max_err]
    images = sorted(images, key=lambda x: x[0])
    return images


def parse_arguments():
    missions = ('rose', 'didy1n', 'didy1w', 'didy2n', 'didy2w')

    parser = argparse.ArgumentParser(description='Asteroid Image Data Generator')
    parser.add_argument('--cache', '-c', metavar='DIR', default=CACHE_DIR,
                        help='path to cache dir (default: %s), ./[mission]/[id] is added to the path' % CACHE_DIR)
    parser.add_argument('--output', '-o', metavar='DIR', default=None,
                        help='path to output dir, default: %s/[mission]/final-[id]' % CACHE_DIR)
    parser.add_argument('--mission', '-m', metavar='M', default='rose', choices=missions,
                        help='mission: %s (default: rose)' % (' | '.join(missions)))
    parser.add_argument('--count', '-n', default='10', type=str, metavar='N',
                        help='number of images to be generated, accepts also format [start:end]')
    parser.add_argument('--id', '-i', default=None, type=str, metavar='N',
                        help='a unique dataset id, defaults to a hash calculated from image generation parameters')

    parser.add_argument('--max-rot-err', default=10, type=float, metavar='A',
                        help='Max rotation error (in deg) allowed when determining pose with AKAZE-PnP-RANSAC (default: %f)' % 10)

    parser.add_argument('--sm-noise', default=0, type=float, metavar='SD',
                        help='Shape model noise level (default: %f)' % 0)
    parser.add_argument('--sm-noise-len-sc', default=SHAPE_MODEL_NOISE_LEN_SC, type=float, metavar='SC',
                        help='Shape model noise length scale (default: %f)' % SHAPE_MODEL_NOISE_LEN_SC)

    parser.add_argument('--tx-noise', default=0, type=float, metavar='SD',
                        help='Texture noise level (default: %f)' % 0)
    parser.add_argument('--tx-noise-len-sc', default=SHAPE_MODEL_NOISE_LEN_SC, type=float, metavar='SC',
                        help='Texture noise length scale (default: %f)' % SHAPE_MODEL_NOISE_LEN_SC)

    parser.add_argument('--haze', default=0.0, type=float, metavar='HZ',
                        help='Max haze brightness (uniform-dist) (default: %f)' % 0.0)

    parser.add_argument('--jets', default=0.0, type=float, metavar='JN',
                        help='Average jet count (exp-distr) (default: %f)' % 0.0)
    parser.add_argument('--jet-int-mode', '--jm', default=0.2, type=float, metavar='JM',
                        help='Jet intensity mode [0, 1], beta-distributed, (default: %f)' % 0.2)
    parser.add_argument('--jet-int-conc', '--jc', default=10, type=float, metavar='JC',
                        help='Jet intensity concentration [1, 1000] (default: %f)' % 10)

    parser.add_argument('--hapke-noise', '--hn', default=0.0, type=float, metavar='SD',
                        help=('Randomize all Hapke reflection model parameters by multiplying with log normally'
                              ' distributed random variable with given SD (default: %f)') % 0.0)
    parser.add_argument('--hapke-th-sd', '--h1', default=None, type=float, metavar='SD',
                        help='Override Hapke effective roughness, th_p [deg] param noise sd (default: %f)' % 0.0)
    parser.add_argument('--hapke-w-sd', '--h2', default=None, type=float, metavar='SD',
                        help='Override Hapke single scattering albedo, w [0, 1] param noise sd (default: %f)' % 0.0)
    parser.add_argument('--hapke-b-sd', '--h3', default=None, type=float, metavar='SD',
                        help='Override Hapke SPPF asymmetry parameter, b [-1, 1] param noise sd (default: %f)' % 0.0)
    parser.add_argument('--hapke-c-sd', '--h4', default=None, type=float, metavar='SD',
                        help='Override Hapke SPPF asymmetry parameter, b [0, 1] param noise sd (default: %f)' % 0.0)
    parser.add_argument('--hapke-shoe', '--h5', default=None, type=float, metavar='SD',
                        help='Override Hapke amplitude of shadow-hiding opposition effect (SHOE), B_SH0 [>=0] param noise sd (default: %f)' % 0.0)
    parser.add_argument('--hapke-shoe-w', '--h6', default=None, type=float, metavar='SD',
                        help='Override Hapke angular half width of SHOE [rad] param noise sd (default: %f)' % 0.0)
    parser.add_argument('--hapke-cboe', '--h7', default=None, type=float, metavar='SD',
                        help='Override Hapke amplitude of coherent backscatter opposition effect (CBOE), B_CB0 [>=0] param noise sd (default: %f)' % 0.0)
    parser.add_argument('--hapke-cboe-w', '--h8', default=None, type=float, metavar='SD',
                        help='Override Hapke angular half width of CBOE [rad] param noise sd (default: %f)' % 0.0)

    args = parser.parse_args()
    return args


if __name__ == '__main__':
    if 0:
        main()
    else:
        sample_mosaic()
