import re
import sys

import cv2

from algo import tools
from settings import *

if __name__ == '__main__':
    try:
        folder = os.path.join(LOG_DIR, sys.argv[1])
        assert os.path.isdir(folder), 'invalid folder path given: %s'%(folder,)
        regex = sys.argv[2]
        target_file = sys.argv[3]
        dw, dh = [int(s) for s in sys.argv[4].split('x')]
        framerate = int(sys.argv[5])
        skip_mult = int(sys.argv[6]) if len(sys.argv) >= 7 else 1
    except Exception as e:
        print(str(e))
        print('\nUSAGE: %s <log-dir relative input-dir> <img_regex> <target_file> <WxH> <framerate> [skip mult]'%(sys.argv[0],))
        quit()

    test = re.compile(regex)
    img_files = []
    for file in os.listdir(folder):
        if test.match(file):
            img_files.append(file)

    img_files = sorted(img_files)
    assert len(img_files)>3, 'too few images found: %s'%(img_files,)

    img0 = cv2.imread(os.path.join(folder, img_files[0]), cv2.IMREAD_COLOR)
    sh, sw, sc = img0.shape
    codecs = ['DIVX', 'H264', 'MPEG', 'MJPG']
    writer = cv2.VideoWriter(target_file, cv2.VideoWriter_fourcc(*codecs[0]), framerate, (dw, dh))
    try:
        for i, f in enumerate(img_files):
            if i % skip_mult == 0:
                tools.show_progress(len(img_files)//skip_mult, i//skip_mult)
                img = cv2.imread(os.path.join(folder, f), cv2.IMREAD_COLOR)
                if sw != dw or sh != dh:
                    img = cv2.resize(img, (dw, dh))
                writer.write(img)
    finally:
        writer.release()
