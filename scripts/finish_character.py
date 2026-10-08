"""Bake source artwork onto a cleaned static TripoSR character; retain inferred back colors."""
import bpy
import bmesh
import json
import math
from pathlib import Path
import sys
from mathutils import Matrix, Vector

mesh_path, source_path, out_dir = map(Path,sys.argv[sys.argv.index('--')+1:])
out_dir.mkdir(parents=True,exist_ok=True)
name=out_dir.name
bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.import_scene.gltf(filepath=str(mesh_path))
objects=[o for o in bpy.context.scene.objects if o.type=='MESH']
assert objects
for obj in objects:obj.matrix_world=Matrix.Rotation(-math.pi/2,4,'X') @ obj.matrix_world
bpy.context.view_layer.update()
pts=[o.matrix_world@Vector(c) for o in objects for c in o.bound_box]
low=Vector(tuple(min(p[i] for p in pts) for i in range(3)))
high=Vector(tuple(max(p[i] for p in pts) for i in range(3)))
center=(low+high)/2;scale=2/(high.z-low.z)
transform=Matrix.Diagonal((scale,scale,scale,1))@Matrix.Translation(-center)
for obj in objects:obj.matrix_world=transform@obj.matrix_world
bpy.ops.object.select_all(action='DESELECT')
for obj in objects:obj.select_set(True)
bpy.context.view_layer.objects.active=objects[0]
bpy.ops.object.join()
obj=bpy.context.object;obj.name=name
bpy.ops.object.transform_apply(location=True,rotation=True,scale=True)
bm=bmesh.new();bm.from_mesh(obj.data)
bmesh.ops.remove_doubles(bm,verts=list(bm.verts),dist=0.00001)
bmesh.ops.recalc_face_normals(bm,faces=list(bm.faces))
bm.to_mesh(obj.data);bm.free();obj.data.update()
for p in obj.data.polygons:p.use_smooth=True
reference=bpy.data.images.load(str(source_path));reference.pack()
source=bpy.data.images.load(str(out_dir/'projection-paint.png'));source.pack()
info=json.loads((out_dir/'source-bounds.json').read_text())
w,h=info['size'];left,top,right,bottom=info['bounds']
ys=[v.co.y for v in obj.data.vertices];zs=[v.co.z for v in obj.data.vertices]
ymin,ymax=min(ys),max(ys);zmin,zmax=min(zs),max(zs)
uv=obj.data.uv_layers.new(name='ArtworkProjection')
for loop in obj.data.loops:
    co=obj.data.vertices[loop.vertex_index].co
    u=(left+(co.y-ymin)/(ymax-ymin)*(right-left))/w
    v=1-(bottom-(co.z-zmin)/(zmax-zmin)*(bottom-top))/h
    uv.data[loop.index].uv=(u,v)
atlas=obj.data.uv_layers.new(name='PaintUV')
obj.data.uv_layers.active=atlas
bpy.ops.object.mode_set(mode='EDIT');bpy.ops.mesh.select_all(action='SELECT')
bpy.ops.uv.smart_project(angle_limit=math.radians(66),island_margin=0.015)
bpy.ops.object.mode_set(mode='OBJECT')
material=bpy.data.materials.new(name+' source-paint bake');material.use_nodes=True
obj.data.materials.clear();obj.data.materials.append(material)
nodes=material.node_tree.nodes;nodes.clear();links=material.node_tree.links
out=nodes.new('ShaderNodeOutputMaterial');emit=nodes.new('ShaderNodeEmission')
links.new(emit.outputs[0],out.inputs['Surface'])
paint=nodes.new('ShaderNodeTexImage');paint.image=source;paint.extension='CLIP'
proj=nodes.new('ShaderNodeUVMap');proj.uv_map='ArtworkProjection'
links.new(proj.outputs['UV'],paint.inputs['Vector'])
color=nodes.new('ShaderNodeVertexColor');color.layer_name=obj.data.color_attributes[0].name
geometry=nodes.new('ShaderNodeNewGeometry')
dot=nodes.new('ShaderNodeVectorMath');dot.operation='DOT_PRODUCT';dot.inputs[1].default_value=(1,0,0)
links.new(geometry.outputs['Normal'],dot.inputs[0])
fade=nodes.new('ShaderNodeMapRange');fade.clamp=True
fade.inputs['From Min'].default_value=-0.2;fade.inputs['From Max'].default_value=0.25
links.new(dot.outputs['Value'],fade.inputs['Value'])
mask=nodes.new('ShaderNodeMath');mask.operation='MULTIPLY'
links.new(fade.outputs['Result'],mask.inputs[0]);links.new(paint.outputs['Alpha'],mask.inputs[1])
mix=nodes.new('ShaderNodeMixRGB');mix.blend_type='MIX'
links.new(mask.outputs[0],mix.inputs[0]);links.new(color.outputs['Color'],mix.inputs[1]);links.new(paint.outputs['Color'],mix.inputs[2])
links.new(mix.outputs[0],emit.inputs['Color'])
texture=bpy.data.images.new(name+' Albedo 2048',width=2048,height=2048,alpha=False)
target=nodes.new('ShaderNodeTexImage');target.image=texture;nodes.active=target
scene=bpy.context.scene;scene.render.engine='CYCLES';scene.cycles.device='CPU';scene.cycles.samples=1
scene.render.bake.margin=16
bpy.ops.object.bake(type='EMIT')
texture.filepath_raw=str(out_dir/'albedo.png');texture.file_format='PNG';texture.save();texture.pack()
nodes.clear();out=nodes.new('ShaderNodeOutputMaterial');bsdf=nodes.new('ShaderNodeBsdfPrincipled')
bsdf.inputs['Roughness'].default_value=0.82
tex=nodes.new('ShaderNodeTexImage');tex.image=texture
uvnode=nodes.new('ShaderNodeUVMap');uvnode.uv_map='PaintUV'
links.new(uvnode.outputs[0],tex.inputs['Vector']);links.new(tex.outputs['Color'],bsdf.inputs['Base Color'])
links.new(tex.outputs['Color'],bsdf.inputs['Emission Color']);bsdf.inputs['Emission Strength'].default_value=0.25
links.new(bsdf.outputs[0],out.inputs['Surface'])
material.name=name+' Painted matte'
smooth=obj.modifiers.new('Gentle surface cleanup','SMOOTH');smooth.factor=0.12;smooth.iterations=2
sub=obj.modifiers.new('Display surface','SUBSURF');sub.levels=1;sub.render_levels=1
obj['source_image']=str(source_path);obj['rig_status']='static display mesh; unrigged'
obj['surface_notes']='Source artwork projected onto front; hidden surfaces inferred by TripoSR. Visual review pending.'
obj['texture_resolution']=2048
bpy.ops.object.camera_add(location=(6,0,0.1));camera=bpy.context.object;camera.data.type='ORTHO';camera.data.ortho_scale=2.35
camera.rotation_euler=(-camera.location).to_track_quat('-Z','Y').to_euler();scene.camera=camera
for pos,power in [((4,-4,5),450),((3,4,1),250)]:
    bpy.ops.object.light_add(type='AREA',location=pos);o=bpy.context.object;o.data.energy=power;o.data.size=5
    o.rotation_euler=(-o.location).to_track_quat('-Z','Y').to_euler()
world=bpy.data.worlds.new('Neutral studio');world.use_nodes=True
world.node_tree.nodes['Background'].inputs[0].default_value=(0.18,0.18,0.18,1);world.node_tree.nodes['Background'].inputs[1].default_value=0.5;scene.world=world
scene.view_settings.view_transform='Standard'
scene.render.resolution_x=768;scene.render.resolution_y=1024;scene.render.resolution_percentage=100
scene.cycles.samples=16;scene.cycles.use_denoising=True
bpy.ops.object.select_all(action='DESELECT');obj.select_set(True);bpy.context.view_layer.objects.active=obj
for area in bpy.context.screen.areas:
    if area.type=='VIEW_3D':
        area.spaces.active.shading.type='MATERIAL'
        area.spaces.active.region_3d.view_distance=3.2
        area.spaces.active.region_3d.view_location=(0,0,0)
        area.spaces.active.region_3d.view_rotation=camera.rotation_euler.to_quaternion()
blend=out_dir/(name+'.blend')
bpy.ops.file.pack_all();bpy.ops.wm.save_as_mainfile(filepath=str(blend))
bpy.ops.export_scene.gltf(filepath=str(out_dir/(name+'.glb')),export_format='GLB',use_selection=True,export_apply=True)
bpy.ops.wm.open_mainfile(filepath=str(blend))
character=bpy.data.objects[name];assert character.type=='MESH' and character.data.uv_layers.get('PaintUV')
assert any(i.packed_file for i in bpy.data.images if i.name.startswith(name+' Albedo'))
bm=bmesh.new();bm.from_mesh(character.data)
nonmanifold=sum(not e.is_manifold for e in bm.edges);bm.free()
report=dict(status='technically_valid',vertices=len(character.data.vertices),faces=len(character.data.polygons),nonmanifold_edges=nonmanifold,
            texture=[2048,2048],packed=True,rigged=False,visual_review='pending',hidden_surfaces='inferred',source=str(source_path))
(out_dir/'validation.json').write_text(json.dumps(report,indent=2)+'\n')
for label,position in [('front',(6,0,0.1)),('three-quarter',(5,-3,0.3)),('back',(-6,0,0.1))]:
    scene=bpy.context.scene;camera=scene.camera;camera.location=position
    camera.rotation_euler=(-camera.location).to_track_quat('-Z','Y').to_euler()
    scene.render.filepath=str(out_dir/(label+'.png'));bpy.ops.render.render(write_still=True)
print(json.dumps(report))
