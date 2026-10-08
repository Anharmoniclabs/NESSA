"""Extend visible edge colors for projection without replacing source artwork."""
import json
from pathlib import Path
import sys
import numpy as np
from PIL import Image
from scipy.ndimage import distance_transform_edt

source,out=map(Path,sys.argv[1:]);out.mkdir(parents=True,exist_ok=True)
im=Image.open(source).convert('RGBA');array=np.array(im)
valid=array[:,:,3]>8
if not valid.any():raise ValueError('Empty artwork')
ys,xs=np.where(valid)
box=[int(xs.min()),int(ys.min()),int(xs.max()+1),int(ys.max()+1)]
(out/'source-bounds.json').write_text(json.dumps({'size':im.size,'bounds':box}))
indices=distance_transform_edt(~valid,return_distances=False,return_indices=True)
rgb=array[:,:,:3][tuple(indices)]
Image.fromarray(rgb).save(out/'projection-paint.png')
