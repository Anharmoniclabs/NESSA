"""Run with Nessa's blender_run tool from a project: creates an editable 1s preview.
This is a pipeline smoke scene, not a final production asset.
"""
from pathlib import Path
import math
import bpy
from mathutils import Vector

out = Path.cwd()/'renders'/'preview'
out.mkdir(parents=True, exist_ok=True)
bpy.ops.object.select_all(action='SELECT')
bpy.ops.object.delete(use_global=False)
scene = bpy.context.scene
scene.render.engine = 'CYCLES'
scene.cycles.device = 'CPU'
scene.cycles.samples = 8
scene.render.resolution_x = 320
scene.render.resolution_y = 180
scene.render.resolution_percentage = 100
scene.render.fps = 12
scene.frame_start, scene.frame_end = 1, 12
scene.render.image_settings.file_format = 'PNG'
scene.render.film_transparent = False
scene.view_settings.view_transform = 'AgX'
scene.world.color = (0.12,0.12,0.12)


def material(name, color, metallic=0):
    mat = bpy.data.materials.new(name)
    mat.use_nodes = True
    bsdf = mat.node_tree.nodes.get('Principled BSDF')
    bsdf.inputs['Base Color'].default_value = (*color,1)
    bsdf.inputs['Metallic'].default_value = metallic
    bsdf.inputs['Roughness'].default_value = 0.28
    return mat


bpy.ops.mesh.primitive_torus_add(major_segments=48,minor_segments=16,location=(0,0,1.3))
hero = bpy.context.object
hero.name = 'Hero_gold_ring'
hero.data.materials.append(material('Warm gold',(0.65,0.33,0.08),0.75))
for polygon in hero.data.polygons:
    polygon.use_smooth = True
for frame,angle in ((1,0.2),(12,1.3)):
    hero.rotation_euler = (angle,0.35,angle*0.3)
    hero.keyframe_insert(data_path='rotation_euler',frame=frame)
bpy.ops.mesh.primitive_plane_add(size=200)
bpy.context.object.data.materials.append(material('Teal stage',(0.015,0.09,0.085)))
for name,location,power,size in [('Key',(2,-3,5),700,4),('Rim',(-3,1,4),900,3)]:
    bpy.ops.object.light_add(type='AREA',location=location)
    light = bpy.context.object
    light.name = name
    light.data.energy,light.data.shape,light.data.size = power,'DISK',size
    light.rotation_euler = (Vector((0,0,1))-light.location).to_track_quat('-Z','Y').to_euler()
bpy.ops.object.camera_add(location=(4,-6,3.4))
scene.camera = bpy.context.object
scene.camera.rotation_euler = (Vector((0,0,1.2))-scene.camera.location).to_track_quat('-Z','Y').to_euler()
scene.camera.data.lens = 48
scene.render.filepath = str(out/'frame_')
bpy.ops.wm.save_as_mainfile(filepath=str(out/'scene.blend'))
bpy.ops.render.render(animation=True)
assert len(list(out.glob('frame_*.png'))) == 12
print('NESSA_PREVIEW_OK: 12 PNG frames and editable scene.blend')
