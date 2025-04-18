import os
import argparse
import h5py
import numpy as np
import re

# based on https://stackoverflow.com/a/6512463/3254658
def parse_bead_list(string):
    m = re.match(r'(\d+)(?:-(\d+))?$', string)
    if not m:
        raise argparse.ArgumentTypeError("'" + string + "' is not a range of number. Expected forms like '0-5' or '2'.")
    start = int(m.group(1), base=10)
    end = int(m.group(2), base=10) or start
    if end < start:
        raise argparse.ArgumentTypeError(f"Start value ({start}) should be larger than final value ({end})")
    return list(range(start, end + 1))

def get_centers(positions, box):
    centers = np.empty((0,positions.shape[2]))
    # based on the position of the first atom get minimal distances
    for frame in range(positions.shape[0]):
        deltas = positions[frame,1:,:]-positions[frame,0,:]
        subtract = np.where(deltas > 0.5 * box, True, False)
        add = np.where(-deltas > 0.5 * box, True, False)

        newpos = np.where(subtract, positions[frame,1:,:]-box, positions[frame,1:,:])
        newpos = np.where(add, positions[frame,1:,:]+box, newpos[:,:])
        newpos = np.insert(newpos, 0, positions[frame,0,:], axis=0)
        centers = np.append(centers, [newpos.mean(axis=0)], axis=0) # get the centroid

    return centers

def center_trajectory(h5md_file, bead_list, overwrite=False, out_path=None):
    if out_path is None:
        out_path = os.path.join(os.path.abspath(os.path.dirname(h5md_file)),
                                os.path.splitext(os.path.split(h5md_file)[-1])[0]+'_new'
                               +os.path.splitext(os.path.split(h5md_file)[-1])[1])
    if os.path.exists(out_path) and not overwrite:
        error_str = (f'The specified output file {out_path} already exists. '
                     f'use overwrite=True ("-f" flag) to overwrite.')
        raise FileExistsError(error_str)

    f_in = h5py.File(h5md_file, 'r')
    f_out = h5py.File(out_path, 'w')

    for k in f_in.keys():
        f_in.copy(k, f_out)

    box_size = f_in['particles/all/box/edges'][:]

    beads_pos = f_in['particles/all/position/value'][:][:,bead_list,:]
    centers = get_centers(beads_pos, box_size)

    translate = (0.5 * box_size) - centers

    translations = np.repeat(translate[:,np.newaxis,:], 
                             f_in['particles/all/position/value'].shape[1], 
                             axis=1)

    tpos = f_in['particles/all/position/value'] + translations
    f_out['particles/all/position/value'][:] = np.mod(tpos, box_size)

    f_in.close()
    f_out.close()


if __name__ == '__main__':
    description = 'Center geometric center of beads in the box for each frame in a .H5 trajectory'
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument('h5md_file', type=str, help='input .H5MD file name')
    parser.add_argument('-b', '--beads', type=parse_bead_list, nargs='+', required=True,
                        help='bead list to center (e.g.: 1-100 102-150)')
    parser.add_argument('-o', '--out', type=str, default=None, dest='out_path',
                        metavar='file name', help='output hymd HDF5 file name')
    parser.add_argument('-f', action='store_true', default=False, dest='force',
                        help='overwrite existing output file')
    args = parser.parse_args()

    bead_list = []
    for interval in args.beads:
        bead_list += interval

    bead_list = np.array(sorted(bead_list))-1

    center_trajectory(args.h5md_file, bead_list, overwrite=args.force, 
                      out_path=args.out_path)
