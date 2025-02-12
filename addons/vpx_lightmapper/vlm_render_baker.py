#    Copyright (C) 2022  Vincent Bousquet
#
#    This program is free software: you can redistribute it and/or modify
#    it under the terms of the GNU General Public License as published by
#    the Free Software Foundation, either version 3 of the License, or
#    (at your option) any later version.
#
#    This program is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU General Public License for more details.
#
#    You should have received a copy of the GNU General Public License
#    along with this program.  If not, see <https://www.gnu.org/licenses/>

import bpy
import math
import mathutils
import bmesh
import os
import re
import time
import gpu
import datetime
import numpy as np
from math import radians
from mathutils import Vector
from gpu_extras.batch import batch_for_shader
from . import vlm_utils
from . import vlm_collections
from PIL import Image # External dependency


def project_point(proj, p):
    p1 = proj @ Vector((p.x, p.y, p.z, 1)) # projected coordinates (range [-1, 1]x[-1, 1])
    return Vector(((1 + p1.x / p1.w) / 2, (1 - p1.y / p1.w) / 2)) # pixel coordinates (range [0, 1]x[0, 1])


def get_light_influence_radius(light):
    """Evaluate the radius of influence of the given object (light or emissive mesh)
    If evaluation fails, return (None, None) otherwise, returns (center, radius)
    Computed based on mesures on real renders, per radius, for 1/10/100/1000 energy
    """
    if not light.vlmSettings.enable_aoi:
        return (None, None)
    if light.type == 'LIGHT':
        light_aois = {
            0.01: [0.200, 1.069, 2.019, 4.314, 10.00],
            0.05: [1.093, 1.093, 2.139, 4.434, 10.00],
            0.10: [1.214, 1.214, 2.223, 4.656, 10.00],
        }
        emission_strength = 0
        has_emission = False
        if light.data.use_nodes:
            for n in [n for n in light.data.node_tree.nodes if n.bl_idname == 'ShaderNodeEmission']:
                if n.inputs['Strength'].is_linked:
                    return (None, None) # Strength is not a constant (Unsupported)
                has_emission = True
                emission_strength += n.inputs['Strength'].default_value
        if not has_emission: emission_strength = 1
        if light.data.type == 'POINT' or light.data.type == 'SPOT':
            radius = light.data.shadow_soft_size
            emission = emission_strength * light.data.energy
            p = max(0, 1 + math.log10(emission))
            if p < 4:
                i = math.floor(p)
                a = p - i
                if radius <= 0.01:
                    r = 0.01
                elif radius <= 0.05:
                    r = 0.05
                else:
                    r = 0.10
                aoi_radius = (1-a)*light_aois[r][i] + a*light_aois[r][i+1]
                #print(f'L {light.name:>20} {radius} {r} {i} {a} => {aoi_radius}')
                return (light.matrix_world @ mathutils.Vector((0,0,0)), aoi_radius)
    elif light.type == 'MESH' or light.type == 'CURVE':
        mesh_aois = {
            0.01: [0.240, 0.252, 0.409, 0.625],
            0.05: [0.613, 1.202, 2.223, 4.530],
            0.10: [0.961, 1.790, 3.436, 6.140],
        }
        emission = radius = 0
        for mat in light.data.materials:
            for n in [n for n in mat.node_tree.nodes if n.bl_idname == 'ShaderNodeEmission']:
                if n.inputs['Strength'].is_linked: 
                    return (None, None) # Strength is not a constant (Unsupported)
                emission += n.inputs['Strength'].default_value
        if emission > 0:
            p = math.log10(emission)
            if p < 3:
                i = math.floor(p)
                a = p - i
                center = mathutils.Vector((0, 0, 0))
                for corner in light.bound_box:
                    center = center + light.matrix_world @ mathutils.Vector(corner)
                center = center * (1.0 / len(light.bound_box))
                for corner in light.bound_box:
                    l = (light.matrix_world @ mathutils.Vector(corner) - center).length
                    radius = max(radius, l)
                if radius <= 0.01:
                    r = 0.01
                elif radius <= 0.05:
                    r = 0.05
                else:
                    r = 0.10
                aoi_radius = (1-a)*mesh_aois[r][i] + a*mesh_aois[r][i+1]
                #print(f'M {light.name:>20} {radius} {r} {i} {a} => {aoi_radius}')
                return (center, aoi_radius)
    return (None, None)


def get_light_influence(scene, depsgraph, camera, light, group_mask):
    """Compute area of influence of the given light
    If a group mask is provided, the AOI is filtered against it
    The implementation use a 2D ellipsoid influence bound computed by projecting a 3D sphere bound
    """
    if not group_mask:
        w = scene.render.resolution_x
        h = scene.render.resolution_y
        mask = None
    else:
        w, h, mask = group_mask
        
    center, radius = get_light_influence_radius(light)
    if center is None:
        return (0, 1, 0, 1)
    
    modelview_matrix = camera.matrix_world.inverted()
    projection_matrix = camera.calc_matrix_camera(
        depsgraph,
        x = scene.render.resolution_x,
        y = scene.render.resolution_y,
        scale_x = scene.render.pixel_aspect_x,
        scale_y = scene.render.pixel_aspect_y,
    )
    proj = projection_matrix @ modelview_matrix

    aoi = (
        max(0, project_point(proj, center + Vector((-radius, 0, 0))).x),
        min(1, project_point(proj, center + Vector(( radius, 0, 0))).x),
        max(0, project_point(proj, center + camera.rotation_quaternion @ Vector((0,  radius, 0))).y), 
        min(1, project_point(proj, center + camera.rotation_quaternion @ Vector((0, -radius, 0))).y))

    if aoi[1] <= aoi[0] or aoi[3] <= aoi[2]:
        return None
    
    if not mask: # No mask, just return the bounds of the area of influence of the light
        return aoi

    min_x = int(aoi[0] * (w-1))
    max_x = int(aoi[1] * (w-1))
    min_y = int(aoi[2] * (h-1))
    max_y = int(aoi[3] * (h-1))
    light_center = project_point(proj, center)
    light_center.x *= w - 1
    light_center.y *= h - 1
    alpha_y = (max_y - min_y) / (max_x - min_x)
    max_r2 = (max_x - min_x) * (max_x - min_x) / 4
    for y in range(min_y, max_y + 1):
        py = (y - light_center.y) * alpha_y
        py2 = py * py
        for x in range(min_x, max_x + 1):
            px = x - light_center.x
            if px*px+py2 < max_r2 and mask[x + y * w] > 0: # inside the influence elipsoid, with an influenced object
                return aoi
    return None


def check_min_render_size(scene):
    w = scene.render.border_max_x - scene.render.border_min_x
    if int(w * scene.render.resolution_x) < 1:
        return False
    h = scene.render.border_max_y - scene.render.border_min_y
    if int(h * scene.render.resolution_y) < 1:
        return False
    return True


def setup_light_scenario(scene, depsgraph, camera, scenario, group_mask, render_col):
    """Apply a light scenario for rendering, returning the previous state and a lambda to restore it
    """
    name, is_lightmap, light_col, lights = scenario
    prev_world = scene.world
    if is_lightmap:
        scene.render.use_border = True
        scene.world = light_col.vlmSettings.world
        scene.render.image_settings.color_mode = 'RGB'
        if light_col.vlmSettings.world:
            scene.render.border_min_x = 0
            scene.render.border_max_x = 1
            scene.render.border_min_y = 0
            scene.render.border_max_y = 1
        else:
            influence = None
            for light in lights:
                light_influence = get_light_influence(scene, depsgraph, camera, light, group_mask)
                if light_influence:
                    if influence:
                        min_x, max_x, min_y, max_y = influence
                        min_x2, max_x2, min_y2, max_y2 = light_influence
                        influence = (min(min_x, min_x2), max(max_x, max_x2), min(min_y, min_y2), max(max_y, max_y2))
                    else:
                        influence = light_influence
            if not influence:
                return None, None
            min_x, max_x, min_y, max_y = influence
            scene.render.border_min_x = min_x
            scene.render.border_max_x = max_x
            scene.render.border_min_y = 1 - max_y
            scene.render.border_max_y = 1 - min_y
            print(f". light scenario '{name}' influence area computed to: {influence}")
            if not check_min_render_size(scene):
                print(f". light scenario '{name}' has no render region, skipping (influence area: {influence})")
                return None, None
        if vlm_utils.is_rgb_led(lights):
            colored_lights = [o for o in lights if o.type=='LIGHT']
            prev_colors = [o.data.color for o in colored_lights]
            for o in colored_lights: o.data.color = (1.0, 1.0, 1.0)
            initial_state = (2, lights, colored_lights, prev_colors)
        else:
            initial_state = (1, lights)
    else:
        scene.render.use_border = False
        scene.world = light_col.vlmSettings.world
        scene.render.image_settings.color_mode = 'RGBA'
        initial_state = (0, lights)
    for light in lights:
        render_col.objects.link(light)
    return initial_state, lambda initial_state : restore_light_setup(initial_state, render_col, lights, scene, prev_world)


def restore_light_setup(initial_state, render_col, lights, scene, prev_world):
    """Restore state after setting up a light scenario for rendering
    """
    scene.world = prev_world
    for light in lights:
        render_col.objects.unlink(light)
    if initial_state[0] == 2: # RGB led, restore colors
        for obj, color in zip(initial_state[2], initial_state[3]): obj.data.color = color

    
def render_all_groups(op, context):
    """Render all render groups for all lighting situations
    """
    if context.blend_data.filepath == '':
        op.report({'ERROR'}, 'You must save your project before rendering')
        return {'CANCELLED'}

    if context.scene.vlmSettings.layback_mode == 'deform':
        op.report({'ERROR'}, 'Deform camera mode is not supported by the lightmapper')
        return {'CANCELLED'}

    bake_col = vlm_collections.get_collection(context.scene.collection, 'VLM.Bake', create=False)
    if not bake_col:
        op.report({'ERROR'}, "No 'VLM.Bake' collection to process")
        return {'CANCELLED'}

    light_col = vlm_collections.get_collection(context.scene.collection, 'VLM.Lights', create=False)
    if not light_col:
        op.report({'ERROR'}, "No 'VLM.Lights' collection to process")
        return {'CANCELLED'}

    camera_object = vlm_utils.get_vpx_item(context, 'VPX.Camera', 'Bake', single=True)
    if not camera_object:
        op.report({'ERROR'}, 'Bake camera is missing')
        return {'CANCELLED'}

    start_time = time.time()
    bakepath = vlm_utils.get_bakepath(context, type='RENDERS')
    vlm_utils.mkpath(bakepath)
    if context.scene.vlmSettings.max_lighting == 0:
        max_scenarios_in_batch = 1024
    else:
        max_scenarios_in_batch = int(context.scene.vlmSettings.max_lighting * 4096 / int(context.scene.vlmSettings.render_height))
    opt_force_render = False # Force rendering even if cache is available
    render_aspect_ratio = context.scene.vlmSettings.render_aspect_ratio
    n_render_groups = vlm_utils.get_n_render_groups(context)
    light_scenarios = vlm_utils.get_lightings(context)
    bake_info_group = bpy.data.node_groups.get('VLM.BakeInfo')
    
    fixed_view = bpy.data.node_groups.get('Fixed View Incoming')
    if fixed_view:
        fixed_view.nodes['Incoming'].inputs[0].default_value = camera_object.location

    # Create temp render scene, using the user render settings setup
    scene = bpy.data.scenes.new('VLM.Tmp Scene')
    scene.collection.objects.link(camera_object)
    scene.camera = camera_object
    for prop in bpy.context.scene.render.bl_rna.properties:
        if not prop.is_readonly and prop.identifier not in {'rna_type'}:
            setattr(scene.render, prop.identifier, getattr(context.scene.render, prop.identifier))
    for prop in bpy.context.scene.cycles.bl_rna.properties:
        if not prop.is_readonly and prop.identifier not in {'rna_type'}:
            setattr(scene.cycles, prop.identifier, getattr(context.scene.cycles, prop.identifier))
    scene.render.engine = 'CYCLES'
    scene.render.use_border = False
    scene.render.use_crop_to_border = False
    render_size = vlm_utils.get_render_size(context)
    scene.render.resolution_x = render_size[0]
    scene.render.resolution_y = render_size[1]
    scene.render.film_transparent = True
    scene.view_settings.view_transform = 'Raw'
    scene.view_settings.look = 'None'
    scene.view_layers[0].use_pass_z = False
    scene.use_nodes = False

    # Setup the scene with all the bake objects with indirect render influence
    indirect_col = bpy.data.collections.new('Indirect')
    render_col = bpy.data.collections.new('Render')
    scene.collection.children.link(indirect_col)
    scene.collection.children.link(render_col)
    vlm_collections.find_layer_collection(scene.view_layers[0].layer_collection, indirect_col).indirect_only = True
    for obj in bake_col.all_objects:
        if not obj.vlmSettings.hide_from_others:
            indirect_col.objects.link(obj)
    
    # Load the group masks to filter out the obviously non influenced scenarios
    mask_path = vlm_utils.get_bakepath(context, type='MASKS')
    group_masks = []
    for i in range(n_render_groups):
        im = Image.open(bpy.path.abspath(f'{mask_path}Mask - Group {i} (Padded LD).png'))
        group_masks.append((im.size[0], im.size[1], im.tobytes("raw", "L")))

    # Prepare and report stats
    n_lighting_situations = len(light_scenarios)
    n_render_performed = n_skipped = n_existing = 0
    n_bake_objects = len([obj for obj in bake_col.all_objects if obj.vlmSettings.use_bake])
    n_total_render = (n_render_groups + n_bake_objects) * n_lighting_situations
    print(f'\nEvaluating {n_total_render} renders ({n_render_groups} render groups and {n_bake_objects} bakes for {n_lighting_situations} lighting situations)')
    
    # Perform the actual rendering of all the passes
    if bake_info_group: bake_info_group.nodes['IsBake'].outputs["Value"].default_value = 1.0
    for group_index, group_mask in enumerate(group_masks):
        objects = [obj for obj in bake_col.all_objects if obj.vlmSettings.render_group == group_index and not obj.vlmSettings.use_bake]
        n_objects = len(objects)
        for obj in objects:
            if not obj.vlmSettings.hide_from_others:
                indirect_col.objects.unlink(obj)
            if obj.vlmSettings.bake_mask:
                render_col.objects.link(obj.vlmSettings.bake_mask)
            render_col.objects.link(obj)
        
        #########
        # Blender 3.2+ batch light pass rendering
        #
        # In Blender 3.2, we can render multiple lights at once and save there data separately using light groups for way faster rendering.
        # This needs to use the compositor to performs denoising and save to split file outputs.
        print(f'. Processing batch render for group {group_index}')
        scenarios_to_process = [scenario for scenario in light_scenarios]
        while scenarios_to_process:
            prev_world = scene.world
            render_world = None
            n_scenarios = 0
            scene.use_nodes = True
            scene.view_layers[0].cycles.denoising_store_passes = True
            scene.render.use_file_extension = False

            nodes = scene.node_tree.nodes
            links = scene.node_tree.links
            nodes.clear()
            links.clear()
            rl = nodes.new("CompositorNodeRLayers")
            rl.scene = scene
            rl.location.x = -200
            dec = max_scenarios_in_batch / 2.0
            batch = []
            influence = None
            remaining_scenarios = []
            for i, scenario in enumerate(scenarios_to_process, start=1):
                name, is_lightmap, light_col, lights = scenario
                # Light pass does not work with emitter meshes (consider the scenario as processed for the batch since it will be processed later)
                if next((l for l in lights if l.type != 'LIGHT'), None): 
                    continue
                # One world bake maximum per batch
                if light_col.vlmSettings.world != None: 
                    if render_world is None: 
                        render_world = light_col.vlmSettings.world
                        render_world.lightgroup = name
                    else:
                        remaining_scenarios.append(scenario)
                        continue
                # Maximum number of simultaneous scenario (Blender may crash by out of memory if there are too much)
                if len(batch) >= max_scenarios_in_batch: 
                    remaining_scenarios.append(scenario)
                    continue
                # Do not re-render existing cached renders
                render_path = f'{bakepath}{name} - Group {group_index}.exr'
                if not opt_force_render and os.path.exists(bpy.path.abspath(render_path)):
                    print(f'. Skipping scenario {name} for group {group_index} since it is already rendered and cached')
                    n_existing += 1
                    continue
                # Only render if the scenario influence the objects in the group
                if not is_lightmap or light_col.vlmSettings.world:
                    scenario_influence = (0, 1, 0, 1)
                else:
                    scenario_influence = None
                    for light in lights:
                        light_influence = get_light_influence(scene, context.view_layer.depsgraph, camera_object, light, group_mask)
                        if light_influence:
                            if scenario_influence:
                                min_x, max_x, min_y, max_y = scenario_influence
                                min_x2, max_x2, min_y2, max_y2 = light_influence
                                scenario_influence = (min(min_x, min_x2), max(max_x, max_x2), min(min_y, min_y2), max(max_y, max_y2))
                            else:
                                scenario_influence = light_influence
                if not scenario_influence:
                    print(f'. Skipping scenario {name} since it is not influencing group {group_index}')
                    n_skipped += 1
                    continue

                if influence:
                    min_x, max_x, min_y, max_y = influence
                    min_x2, max_x2, min_y2, max_y2 = scenario_influence
                    influence = (min(min_x, min_x2), max(max_x, max_x2), min(min_y, min_y2), max(max_y, max_y2))
                else:
                    influence = scenario_influence
                
                scene.view_layers[0].lightgroups.add(name=name.replace(".","_"))
                initial_state = (0, None)
                if vlm_utils.is_rgb_led(lights):
                    colored_lights = [o for o in lights if o.type=='LIGHT']
                    prev_colors = [o.data.color for o in colored_lights]
                    for o in colored_lights: o.data.color = (1.0, 1.0, 1.0)
                    initial_state = (1, zip(colored_lights, prev_colors))
                for light in lights:
                    light.lightgroup = name.replace(".","_")
                    render_col.objects.link(light)
                denoise = nodes.new("CompositorNodeDenoise")
                denoise.location.x = 200
                denoise.location.y = -(i-dec) * 200
                links.new(rl.outputs['Denoising Normal'], denoise.inputs['Normal'])
                links.new(rl.outputs['Denoising Albedo'], denoise.inputs['Albedo'])
                out = nodes.new("CompositorNodeOutputFile")
                out.location.x = 600
                out.location.y = -(i-dec) * 200
                if is_lightmap:
                    links.new(denoise.outputs['Image'], out.inputs['Image'])
                else:
                    alpha = nodes.new("CompositorNodeSetAlpha")
                    alpha.location.x = 400
                    alpha.location.y = -(i-dec) * 200
                    links.new(denoise.outputs['Image'], alpha.inputs['Image'])
                    links.new(rl.outputs['Alpha'], alpha.inputs['Alpha'])
                    links.new(alpha.outputs['Image'], out.inputs['Image'])
                batch.append((scenario, denoise, out, initial_state))

            if not batch:
                scenarios_to_process = remaining_scenarios
                continue

            scene.world = render_world

            for scenario, denoise, out, initial_state in batch:
                name, is_lightmap, light_col, lights = scenario
                links.new(rl.outputs[f'Combined_{name.replace(".","_")}'], denoise.inputs[0])

            for scenario, denoise, out, _ in batch:
                name, is_lightmap, light_col, lights = scenario
                out.base_path = f'{bakepath}'
                out.file_slots[0].path = f'{name} - Group {group_index}.exr'
                out.file_slots[0].use_node_format = True
                out.format.file_format = 'OPEN_EXR'
                out.format.color_mode = 'RGB' if is_lightmap else 'RGBA'
                out.format.exr_codec = 'ZIP'
                out.format.color_depth = '16'
            
            elapsed = time.time() - start_time
            msg = f". Rendering group #{group_index+1}/{n_render_groups} ({n_objects} objects) for {len(batch)} lighting scenarios (influence: {influence}). Progress is {((n_skipped+n_render_performed+n_existing)/n_total_render):5.2%}, elapsed: {vlm_utils.format_time(elapsed)}"
            if elapsed > 0 and n_render_performed > 0:
                elapsed_per_render = elapsed / n_render_performed
                remaining_render = n_total_render - (n_skipped+n_render_performed+n_existing)
                msg = f'{msg}, remaining: {vlm_utils.format_time(remaining_render * elapsed_per_render)} for {remaining_render} renders'
            print(msg)
            print(f'. Scenarios: {",".join(s[0][0] for s in batch)}')
            #return {'FINISHED'}                    

            # Setup AOI
            if influence != (0, 1, 0, 1):
                min_x, max_x, min_y, max_y = influence
                scene.render.use_border = True
                scene.render.border_min_x = min_x
                scene.render.border_max_x = max_x
                scene.render.border_min_y = 1 - max_y
                scene.render.border_max_y = 1 - min_y
            else:
                scene.render.use_border = False
            
            bpy.ops.render.render(write_still=False, scene=scene.name)
            n_render_performed += len(batch)

            # Rename files since blender will append a render index number to the filename
            for file in os.listdir(bpy.path.abspath(f'{bakepath}')):
                match = re.fullmatch(r"(.*exr)\d\d\d\d", file)
                if match:
                    outRenderFileName = bpy.path.abspath(f'{bakepath}{match[1]}')
                    if os.path.exists(outRenderFileName):
                        os.remove(outRenderFileName)
                    os.rename(bpy.path.abspath(f'{bakepath}{file}'), outRenderFileName)

            for scenario, denoise, out, initial_state in batch:
                _, _, _, lights = scenario
                for light in lights:
                    render_col.objects.unlink(light)
                if initial_state[0] == 1:
                    for o, c in initial_state[1]: o.data.color = c
                bpy.ops.scene.view_layer_remove_lightgroup({'scene':scene})
            nodes.clear()
            links.clear()
            scene.use_nodes = False
            scene.world = prev_world
            scene.view_layers[0].cycles.denoising_store_passes = False
            scene.render.use_border = False
        
            scenarios_to_process = remaining_scenarios
    
        #########
        # Default rendering
        #
        # Light pass batch rendering does not support emitter mesh, so we use the legacy per light scenario rendering to process them
        for i, scenario in enumerate(light_scenarios, start=1):
            name, is_lightmap, light_col, lights = scenario
            render_path = f'{bakepath}{scenario[0]} - Group {group_index}.exr'
            if opt_force_render or not os.path.exists(bpy.path.abspath(render_path)):
                state, restore_func = setup_light_scenario(scene, context.view_layer.depsgraph, camera_object, scenario, group_mask, render_col)
                elapsed = time.time() - start_time
                msg = f". Rendering group #{group_index+1}/{n_render_groups} ({n_objects} objects) for '{scenario[0]}' ({i}/{n_lighting_situations}). Progress is {((n_skipped+n_render_performed+n_existing)/n_total_render):5.2%}, elapsed: {vlm_utils.format_time(elapsed)}"
                if elapsed > 0 and n_render_performed > 0:
                    elapsed_per_render = elapsed / n_render_performed
                    remaining_render = n_total_render - (n_skipped+n_render_performed+n_existing)
                    msg = f'{msg}, remaining: {vlm_utils.format_time(remaining_render * elapsed_per_render)} for {remaining_render} renders'
                if state:
                    print(msg)
                    scene.render.filepath = render_path
                    scene.render.image_settings.file_format = 'OPEN_EXR'
                    scene.render.image_settings.color_mode = 'RGB' if is_lightmap else 'RGBA'
                    scene.render.image_settings.exr_codec = 'ZIP' # Lossless compression
                    scene.render.image_settings.color_depth = '16'
                    bpy.ops.render.render(write_still=True, scene=scene.name)
                    restore_func(state)
                    print('\n')
                    n_render_performed += 1
                else:
                    print(f'{msg} - Skipped (no influence)')
                    n_skipped += 1

        for obj in objects:
            if not obj.vlmSettings.hide_from_others:
                indirect_col.objects.link(obj)
            if obj.vlmSettings.bake_mask:
                render_col.objects.unlink(obj.vlmSettings.bake_mask)
            render_col.objects.unlink(obj)

    #########
    # Traditional baking
    #
    # Baking using rendering and projective texture gives (surprisingly) good results in most situations but it will look wrong for some
    # objects that will need traditional baking. This is especially true for movable parts like spinners, flipper bats,...
    # These objects will be processed with traditional bake which requires them to be UV unwrapped and to use view point aware materials

    scene.view_settings.view_transform = 'Raw'
    scene.view_settings.look = 'None'
    if bake_info_group: bake_info_group.nodes['IsBake'].outputs["Value"].default_value = 2.0
    for obj in [obj for obj in bake_col.all_objects if obj.vlmSettings.use_bake]:
        if not obj.vlmSettings.hide_from_others:
            indirect_col.objects.unlink(obj)
        render_col.objects.link(obj)
        elapsed = time.time() - start_time
        im = Image.open(bpy.path.abspath(f'{mask_path}Mask - Bake - {obj.name} (Padded LD).png'))
        obj_mask = (im.size[0], im.size[1], im.tobytes("raw", "L"))
        for i, scenario in enumerate(light_scenarios, start=1):
            name, is_lightmap, light_col, lights = scenario
            render_path = f'{bakepath}{scenario[0]} - Bake - {obj.name}.exr'
            if opt_force_render or not os.path.exists(bpy.path.abspath(render_path)):
                state, restore_func = setup_light_scenario(scene, context.view_layer.depsgraph, camera_object, scenario, obj_mask, render_col)
                elapsed = time.time() - start_time
                msg = f". Baking '{obj.name}' for '{scenario[0]}' ({i}/{n_lighting_situations}). Progress is {((n_skipped+n_render_performed+n_existing)/n_total_render):5.2%}, elapsed: {vlm_utils.format_time(elapsed)}"
                if elapsed > 0 and n_render_performed > 0:
                    elapsed_per_render = elapsed / n_render_performed
                    remaining_render = n_total_render - (n_skipped+n_render_performed+n_existing)
                    msg = f'{msg}, remaining: {vlm_utils.format_time(remaining_render * elapsed_per_render)} for {remaining_render} renders'
                if state:
                    print(msg)
                    img_nodes = []
                    bake_img = bpy.data.images.new('Bake', obj.vlmSettings.bake_width, obj.vlmSettings.bake_height, alpha=True, float_buffer=True)
                    for mat in obj.data.materials:
                        ti = mat.node_tree.nodes.new("ShaderNodeTexImage")
                        ti.image = bake_img
                        mat.node_tree.nodes.active = ti
                        img_nodes.append(ti)
                    scene.render.filepath = render_path
                    scene.render.image_settings.file_format = 'OPEN_EXR'
                    scene.render.image_settings.color_mode = 'RGB' if is_lightmap else 'RGBA'
                    scene.render.image_settings.exr_codec = 'ZIP' # Lossless compression
                    scene.render.image_settings.color_depth = '16'
                    with context.temp_override(scene=scene, selected_objects=[obj]):
                        bpy.ops.object.bake(type='COMBINED', margin=context.scene.vlmSettings.padding, use_selected_to_active=False, use_clear=True)
                        bake_img.save_render(bpy.path.abspath(render_path), scene=scene)
                    for mat, ti in zip(obj.data.materials, img_nodes):
                        mat.node_tree.nodes.remove(ti)
                    bpy.data.images.remove(bake_img)
                    restore_func(state)
                    print('\n')
                    n_render_performed += 1
                else:
                    print(f'{msg} - Skipped (no influence)')
                    n_skipped += 1
            else:
                print(f'{msg} - Skipped since it is already rendered and cached')
                n_existing += 1
        if not obj.vlmSettings.hide_from_others:
            indirect_col.objects.link(obj)
        render_col.objects.unlink(obj)

    if bake_info_group: bake_info_group.nodes['IsBake'].outputs["Value"].default_value = 0.0
    bpy.data.scenes.remove(scene)
    length = time.time() - start_time
    print(f"\nRendering finished in a total time of {vlm_utils.format_time(length)}")
    if n_existing > 0: print(f". {n_existing:>3} renders were skipped since they were already existing")
    if n_skipped > 0: print(f". {n_skipped:>3} renders were skipped since objects were outside of lights influence")
    if n_render_performed > 0: print(f". {n_render_performed:>3} renders were computed ({vlm_utils.format_time(length/n_render_performed)} per render)")

    context.scene.vlmSettings.last_bake_step = 'renders'
    return {'FINISHED'}
