"""Blender background import, named source provenance, save and reopen validation."""
import json
from pathlib import Path
import sys
import bpy
from mathutils import Vector, Matrix
import math

mesh_path, blend_path, source_path = map(Path, sys.argv[sys.argv.index('--')+1:])
bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.import_scene.gltf(filepath=str(mesh_path))
meshes = [o for o in bpy.context.scene.objects if o.type == 'MESH']
if not meshes or not all(len(o.data.vertices) and len(o.data.polygons) for o in meshes):
    raise RuntimeError('Imported mesh contains no geometry')
# TripoSR produces Z-up coordinates inside a glTF container; the importer assumes
# glTF Y-up. Undo that conversion before framing the reconstructed character.
rotation=Matrix.Rotation(-math.pi/2,4,'X')
for obj in meshes:
    obj.matrix_world=rotation @ obj.matrix_world
bpy.context.view_layer.update()
points=[obj.matrix_world @ Vector(corner) for obj in meshes for corner in obj.bound_box]
low=Vector(tuple(min(p[i] for p in points) for i in range(3)))
high=Vector(tuple(max(p[i] for p in points) for i in range(3)))
center=(low+high)/2
scale=2/max(high.z-low.z,0.001)
transform=Matrix.Diagonal((scale,scale,scale,1)) @ Matrix.Translation(-center)
for obj in meshes:
    obj.matrix_world=transform @ obj.matrix_world
for i, obj in enumerate(meshes):
    obj.name = mesh_path.parents[1].name + (f'_{i}' if i else '')
    obj['source_image'] = str(source_path)
    obj['reconstruction_model'] = 'stabilityai/TripoSR'
    obj['character_identity'] = 'unassigned — source filename retained'
    obj['rig_status'] = 'unrigged'
    obj['review_status'] = 'needs visual review; hidden surfaces inferred'
bpy.ops.object.camera_add(location=(5,-1,0.5))
camera=bpy.context.object
camera.rotation_euler=(Vector((0,0,0))-camera.location).to_track_quat('-Z','Y').to_euler()
camera.data.type='ORTHO';camera.data.ortho_scale=2.6
bpy.context.scene.camera=camera
for pos, energy, size in [((4,-3,5),700,5),((3,3,2),400,4)]:
    bpy.ops.object.light_add(type='AREA',location=pos)
    lamp=bpy.context.object;lamp.data.energy=energy;lamp.data.shape='DISK';lamp.data.size=size
    lamp.rotation_euler=(-lamp.location).to_track_quat('-Z','Y').to_euler()
world=bpy.data.worlds.new('Studio');world.use_nodes=True
world.node_tree.nodes['Background'].inputs[0].default_value=(0.15,0.15,0.15,1)
bpy.context.scene.world=world
bpy.context.scene.render.engine='CYCLES'
bpy.context.scene.cycles.device='CPU';bpy.context.scene.cycles.samples=4
bpy.context.scene.render.resolution_x=384;bpy.context.scene.render.resolution_y=384
bpy.context.scene.render.resolution_percentage=100
bpy.context.scene['source_image']=str(source_path)
bpy.context.scene['model']='TripoSR; unrigged reconstruction'
bpy.data.images.load(str(source_path),check_existing=True).pack()
bpy.ops.file.pack_all()
bpy.ops.wm.save_as_mainfile(filepath=str(blend_path))
bpy.ops.wm.open_mainfile(filepath=str(blend_path))
checked=[o for o in bpy.context.scene.objects if o.type=='MESH']
assert checked and all(len(o.data.polygons)>0 for o in checked)
bpy.context.scene.render.filepath=str(blend_path.with_suffix('.png'))
bpy.ops.render.render(write_still=True)
report={'status':'verified','meshes':len(checked),'vertices':sum(len(o.data.vertices) for o in checked),
        'faces':sum(len(o.data.polygons) for o in checked),'rigged':False,'visual_review':'pending'}
blend_path.with_suffix('.validation.json').write_text(json.dumps(report,indent=2)+'\n')
print(json.dumps(report))
