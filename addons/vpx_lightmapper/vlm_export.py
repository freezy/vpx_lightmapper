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
import os
import zlib
import struct
from . import biff_io
from . import vlm_utils
from . import vlm_collections

# Dependencies which need a custom install (not included in the Blender install)
import olefile
import pythoncom
import win32crypt
import win32cryptcon
from win32com import storagecon


# TODO
# - Try computing bakemap histogram, select the right format depending on the intensity span (EXR / brightness adjusted PNG or WEBP)
# - Export to static rendering / not active / active according to the part exported (static, translucency, below playfield ?)
# - Sort faces of bakes (not lightmaps) from front to back except for active bakes which should be sorted from back to front


def export_name(object_name):
    return object_name.replace(".", "_").replace(" ", "_").replace("-", "_")


def export_vpx(context):
    """Export bakes by updating the reference VPX file
    . Remove all 'VLM.' prefixed objects from the source file
    . Disable rendering for all baked objects
    . Add all packmaps as texture with 'VLM.' prefixed name
    . Add base materials with 'VLM.' prefixed name
    . Add all bakes as primitives with 'VLM.' prefixed name
    . Update the table script with the needed light/lightmap sync code
    """
    vlmProps = context.scene.vlmSettings
    result_col = vlm_collections.get_collection('BAKE RESULT')
    bakepath = vlm_utils.get_bakepath(context)
    vlm_utils.mkpath(f"{bakepath}Export/")
    input_path = bpy.path.abspath(vlmProps.table_file)
    export_mode = vlmProps.export_mode
    export_image_type = vlmProps.export_image_type
    bake_col = vlm_collections.get_collection('BAKE')
    overlay_col = vlm_collections.get_collection('OVERLAY')
    light_col = vlm_collections.get_collection('LIGHTS')
    if not os.path.isfile(input_path):
        self.report({'WARNING'},f"{input_path} does not exist")
        return {'CANCELLED'}
    
    output_path = bpy.path.abspath(f"//{os.path.splitext(bpy.path.basename(input_path))[0]} - VLM.vpx")
    print(f'\nExporting bake results to {bpy.path.basename(output_path)}')

    src_storage = olefile.OleFileIO(input_path)
    dst_storage = pythoncom.StgCreateStorageEx(output_path, storagecon.STGM_TRANSACTED | storagecon.STGM_READWRITE | storagecon.STGM_SHARE_EXCLUSIVE | storagecon.STGM_CREATE, storagecon.STGFMT_DOCFILE, 0, pythoncom.IID_IStorage, None, None)
    dst_gamestg = dst_storage.CreateStorage("GameStg", storagecon.STGM_DIRECT | storagecon.STGM_READWRITE | storagecon.STGM_SHARE_EXCLUSIVE | storagecon.STGM_CREATE, 0, 0)
    dst_tableinfo = dst_storage.CreateStorage("TableInfo", storagecon.STGM_DIRECT | storagecon.STGM_READWRITE | storagecon.STGM_SHARE_EXCLUSIVE | storagecon.STGM_CREATE, 0, 0)

    crypt_context = win32crypt.CryptAcquireContext(None, None, win32cryptcon.PROV_RSA_FULL, win32cryptcon.CRYPT_VERIFYCONTEXT | win32cryptcon.CRYPT_NEWKEYSET)
    data_hash = crypt_context.CryptCreateHash(win32cryptcon.CALG_MD2)
    data_hash.CryptHashData(b'Visual Pinball')
    def append_structure(src_path, mode, hashed):
        index = 0
        while src_storage.exists(f'{src_path}{index}'):
            file_structure.append((f'{src_path}{index}', mode, hashed))
            index = index + 1
  
    file_structure = [ # path, 0=unstructured bytes/1=BIFF, hashed ?
        ('GameStg/Version', 0, True),
        ('TableInfo/TableName', 0, True),
        ('TableInfo/AuthorName', 0, True),
        ('TableInfo/TableVersion', 0, True),
        ('TableInfo/ReleaseDate', 0, True),
        ('TableInfo/AuthorEmail', 0, True),
        ('TableInfo/AuthorWebSite', 0, True),
        ('TableInfo/TableBlurb', 0, True),
        ('TableInfo/TableDescription', 0, True),
        ('TableInfo/TableRules', 0, True),
        ('TableInfo/TableSaveDate', 0, False),
        ('TableInfo/TableSaveRev', 0, False),
        ('TableInfo/Screenshot', 1, True),
        ('GameStg/CustomInfoTags', 1, True), # custom info tags must be hashed just after this stream
        ('GameStg/GameData', 1, True),]
    #append_structure('GameStg/GameItem', 1, False),
    append_structure('GameStg/Sound', 1, False),
    #append_structure('GameStg/Image', 1, False),
    append_structure('GameStg/Font', 1, False),
    append_structure('GameStg/Collection', 1, True),

    table_lights = []
    table_flashers = []

    # Remove previous baked models and append the new ones, also hide/remove baked items
    n_read_item = 0
    n_game_items = 0
    used_images = {}
    removed_images = {}
    prefix = ['Wall', 'Flipper', 'Timer', 'Plunger', 'Text', 'Bumper', 'Trigger', 'Light', 'Kicker', '', 'Gate', 'Spinner', 'Ramp', 
        'Table', 'LightCenter', 'DragPoint', 'Collection', 'DispReel', 'LightSeq', 'Prim', 'Flasher', 'Rubber', 'Target']
    while src_storage.exists(f'GameStg/GameItem{n_read_item}'):
        data = src_storage.openstream(f'GameStg/GameItem{n_read_item}').read()
        data = bytearray(data)
        item_data = biff_io.BIFF_reader(data)
        item_type = item_data.get_32()
        if item_type < 0 or item_type >= len(prefix):
            print(f'Unsupported item #{n_read_item} type #{item_type}')
            dst_stream = dst_gamestg.CreateStream(f'GameItem{n_game_items}', storagecon.STGM_DIRECT | storagecon.STGM_READWRITE | storagecon.STGM_SHARE_EXCLUSIVE | storagecon.STGM_CREATE, 0, 0)
            dst_stream.Write(data)
            n_game_items += 1
            n_read_item += 1
            continue
        name = 'unknown'
        item_images = []
        is_baked = False
        is_baked_light = False
        is_physics = True
        while not item_data.is_eof():
            item_data.next()
            if item_data.tag == 'NAME':
                name = item_data.get_wide_string()
                is_baked = next((o for o in bake_col.all_objects if o.vlmSettings.vpx_object == name), None) is not None
                is_baked = is_baked or next((o for o in overlay_col.all_objects if o.vlmSettings.vpx_object == name), None) is not None
                is_baked_light = next((o for o in light_col.all_objects if o.vlmSettings.vpx_object == name), None) is not None
                break
            item_data.skip_tag()
        item_data = biff_io.BIFF_reader(data)
        item_type = item_data.get_32()
        while not item_data.is_eof():
            item_data.next()
            reflection_field = visibility_field = False
            is_part_baked = is_baked
            if item_data.tag == 'REEN':
                reflection_field = True
            elif item_data.tag == 'CLDR' or item_data.tag == 'CLDW': # Collidable for wall ramps and primitives
                is_physics = item_data.get_bool()
            elif item_data.tag == 'IMAG' or item_data.tag == 'SIMG' or item_data.tag == 'IMGF' or item_data.tag == 'IMAB':
                item_images.append(item_data.get_string())
            elif (item_type == 0 or item_type == 6) and item_data.tag == 'VSBL': # for wall top (0) and triggers (6)
                visibility_field = True
            elif item_type == 0 and item_data.tag == 'SVBL': # for wall sides (0)
                visibility_field = True
            elif (item_type == 12 or item_type == 21) and item_data.tag == 'RVIS': # for ramps (12) and rubbers (21)
                visibility_field = True
            elif item_type == 10 and item_data.tag == 'GSUP': # for gate (10) bracket, combined with GVSB
                if f'VPX.Gate.Bracket.{name}' in bake_col.all_objects:
                    item_data.put_bool(False)
            elif item_type == 11 and item_data.tag == 'SSUP': # for spinner bracket (11) combined with SVIS
                if f'VPX.Spinner.Bracket.{name}' in bake_col.all_objects:
                    item_data.put_bool(False)
            elif (item_type == 19 or item_type == 22) and item_data.tag == 'TVIS': # for primitives (19) and hit targets (22)
                visibility_field = True
            elif item_type == 20 and item_data.tag == 'FVIS': # for flashers (20)
                visibility_field = True
            elif item_type == 5 and item_data.tag == 'CAVI': # for bumper caps (5)
                is_part_baked = next((o for o in bake_col.all_objects if o.vlmSettings.vpx_object == name and o.name.startswith('VPX.Bumper.Cap.')), None) is not None
                visibility_field = True
            elif item_type == 5 and item_data.tag == 'BSVS': # for bumper base & ring & skirt (5), recent files also have separate fields for ring & skirt
                is_part_baked = next((o for o in bake_col.all_objects if o.vlmSettings.vpx_object == name and o.name.startswith('VPX.Bumper.Base.')), None) is not None
                visibility_field = True
            elif item_type == 5 and item_data.tag == 'RIVS': # for bumper ring (5)
                is_part_baked = next((o for o in bake_col.all_objects if o.vlmSettings.vpx_object == name and o.name.startswith('VPX.Ring.Base.')), None) is not None
                visibility_field = True
            elif item_type == 5 and item_data.tag == 'SKVS': # for bumper skirt (5)
                is_part_baked = next((o for o in bake_col.all_objects if o.vlmSettings.vpx_object == name and o.name.startswith('VPX.Bumper.Socket.')), None) is not None
                visibility_field = True
            elif item_type == 8 and item_data.tag == 'TYPE': # for kicker (8), type 0 is invisible
                pass # FIXME implement
            # Not needed since we do not bake wires / movable part of spinners
            # elif item_type == 10 and item_data.tag == 'GVSB': # for gate (10): overall gate (wire and bracket)
            # elif item_type == 11 and item_data.tag == 'SVIS': # for spinner (11): overall spinner (wire and bracket)
            if item_type == 7:
                table_lights.append(name)
                if is_baked_light:
                    if item_data.tag == 'BULT':
                        item_data.put_bool(True)
                    elif item_data.tag == 'BHHI':
                        item_data.put_float(-28)
            if item_type == 20:
                table_flashers.append(name)
            if visibility_field and is_part_baked:
                item_data.put_bool(False)
            if reflection_field and is_part_baked:
                item_data.put_bool(False)
            item_data.skip_tag()
        remove = (export_mode == 'remove' or export_mode == 'remove_all') and is_baked and not is_physics
        remove = remove or name.startswith('VLM.') or name == 'VLMTimer'
        # Mark images as used or not (if baked)
        if remove or ((export_mode == 'remove' or export_mode == 'remove_all') and is_baked):
            for image in item_images:
                if image not in removed_images:
                    removed_images[image] = [name]
                else:
                    removed_images[image].append(name)
        else:
            for image in item_images:
                if image not in used_images:
                    used_images[image] = [name]
                else:
                    used_images[image].append(name)
        # Filters out object
        if remove:
            print(f'. Item {name:>21s} was removed from export table')
        else:
            dst_stream = dst_gamestg.CreateStream(f'GameItem{n_game_items}', storagecon.STGM_DIRECT | storagecon.STGM_READWRITE | storagecon.STGM_SHARE_EXCLUSIVE | storagecon.STGM_CREATE, 0, 0)
            dst_stream.Write(data)
            n_game_items += 1
        n_read_item = n_read_item + 1

    # Add the sync timer
    writer = biff_io.BIFF_writer()
    writer.write_u32(2)
    writer.write_tagged_vec2(b'VCEN', 0, 0)
    writer.write_tagged_bool(b'TMON', True)
    writer.write_tagged_32(b'TMIN', -1)
    writer.write_tagged_wide_string(b'NAME', 'VLMTimer')
    writer.write_tagged_bool(b'BGLS', False)
    writer.write_tagged_bool(b'LOCK', True)
    writer.write_tagged_u32(b'LAYR', 0)
    writer.write_tagged_string(b'LANR', 'VLM.Visuals')
    writer.write_tagged_bool(b'LVIS', True)
    writer.close()
    dst_stream = dst_gamestg.CreateStream(f'GameItem{n_game_items}', storagecon.STGM_DIRECT | storagecon.STGM_READWRITE | storagecon.STGM_SHARE_EXCLUSIVE | storagecon.STGM_CREATE, 0, 0)
    dst_stream.Write(writer.get_data())
    n_game_items += 1

    # Add new bake models
    for obj in sorted([obj for obj in result_col.all_objects], key=lambda x: f'{x.vlmSettings.bake_type == "lightmap"}-{x.name}'):
        is_light = obj.vlmSettings.bake_type == 'lightmap'
        is_playfield = obj.vlmSettings.bake_type == 'playfield'
        writer = biff_io.BIFF_writer()
        writer.write_u32(19)
        writer.write_tagged_padded_vector(b'VPOS', 0, 0, 0)
        writer.write_tagged_padded_vector(b'VSIZ', 100, 100, 100)
        writer.write_tagged_float(b'RTV0', 0)
        writer.write_tagged_float(b'RTV1', 0)
        writer.write_tagged_float(b'RTV2', 0)
        writer.write_tagged_float(b'RTV3', 0)
        writer.write_tagged_float(b'RTV4', 0)
        writer.write_tagged_float(b'RTV5', 0)
        writer.write_tagged_float(b'RTV6', 0)
        writer.write_tagged_float(b'RTV7', 0)
        writer.write_tagged_float(b'RTV8', 0)
        writer.write_tagged_string(b'IMAG', f'VLM.Packmap{obj.vlmSettings.bake_packmap}')
        writer.write_tagged_string(b'NRMA', '')
        writer.write_tagged_u32(b'SIDS', 4)
        writer.write_tagged_wide_string(b'NAME', export_name(obj.name))
        writer.write_tagged_string(b'MATR', 'VLM.Lightmap' if is_light else 'VLM.Bake.Active') # FIXME we should have 2 variants (active/not active)
        writer.write_tagged_u32(b'SCOL', 0xFFFFFF)
        writer.write_tagged_bool(b'TVIS', not is_playfield)
        writer.write_tagged_bool(b'DTXI', False)
        writer.write_tagged_bool(b'HTEV', False)
        writer.write_tagged_float(b'THRS', 2.0)
        writer.write_tagged_float(b'ELAS', 0.3)
        writer.write_tagged_float(b'ELFO', 0.0)
        writer.write_tagged_float(b'RFCT', 0.0)
        writer.write_tagged_float(b'RSCT', 0.0)
        writer.write_tagged_float(b'EFUI', 0.0)
        writer.write_tagged_float(b'CORF', 0.0)
        writer.write_tagged_bool(b'CLDR', False)
        writer.write_tagged_bool(b'ISTO', True)
        writer.write_tagged_bool(b'U3DM', True)
        writer.write_tagged_bool(b'STRE', False) # not is_light) # FIXME static rendering should be true for solid map without transparency
        writer.write_tagged_u32(b'DILI', 255) # 255 if 1.0 for disable lighting
        writer.write_tagged_float(b'DILB', 1.0) # also disable lighting from below
        writer.write_tagged_bool(b'REEN', False)
        writer.write_tagged_bool(b'EBFC', False)
        writer.write_tagged_string(b'MAPH', '')
        writer.write_tagged_bool(b'OVPH', False)
        writer.write_tagged_bool(b'DIPT', False)
        writer.write_tagged_bool(b'OSNM', False)
        writer.write_tagged_string(b'M3DN', f'VLM.{obj.name}')
        indices = []
        vertices = []
        vert_dict = {}
        n_vertices = 0
        uv_layer_packed = obj.data.uv_layers["UVMap Packed"]
        for poly in obj.data.polygons:
            if len(poly.loop_indices) != 3:
                continue
            for loop_index in reversed(poly.loop_indices):
                loop = obj.data.loops[loop_index]
                x, y, z = obj.data.vertices[loop.vertex_index].co
                nx, ny, nz = loop.normal
                u, v = uv_layer_packed.data[loop_index].uv
                vertex = (x, -y, z, nx, -ny, nz, u, 1.0 - v)
                existing_index = vert_dict.get(vertex, None)
                if existing_index is None:
                    vert_dict[vertex] = n_vertices
                    vertices.extend(vertex)
                    indices.append(n_vertices)
                    n_vertices += 1
                else:
                    indices.append(existing_index)
        n_indices = len(indices)
        print(f'. Adding {obj.name:<15} with {n_vertices:>6} vertices for {int(n_indices/3):>5} faces')
        
        writer.write_tagged_u32(b'M3VN', n_vertices)
        #writer.write_tagged_data(b'M3DX', struct.pack(f'<{len(vertices)}f', *vertices))
        compressed_vertices = zlib.compress(struct.pack(f'<{len(vertices)}f', *vertices))
        writer.write_tagged_u32(b'M3CY', len(compressed_vertices))
        writer.write_tagged_data(b'M3CX', compressed_vertices)
        
        writer.write_tagged_u32(b'M3FN', n_indices)
        if n_vertices > 65535:
            #writer.write_tagged_data(b'M3DI', struct.pack(f'<{n_indices}I', *indices))
            compressed_indices = zlib.compress(struct.pack(f'<{n_indices}I', *indices))
        else:
            #writer.write_tagged_data(b'M3DI', struct.pack(f'<{n_indices}H', *indices))
            compressed_indices = zlib.compress(struct.pack(f'<{n_indices}H', *indices))
        writer.write_tagged_u32(b'M3CJ', len(compressed_indices))
        writer.write_tagged_data(b'M3CI', compressed_indices)
        
        writer.write_tagged_float(b'PIDB', -1000.0 if is_light else 1000.0)
        writer.write_tagged_bool(b'ADDB', is_light) # Additive blending VPX mod
        writer.write_tagged_u32(b'FALP', 100) # Additive blending VPX mod
        writer.write_tagged_bool(b'LOCK', True)
        writer.write_tagged_bool(b'LVIS', True)
        writer.write_tagged_u32(b'LAYR', 0)
        writer.write_tagged_string(b'LANR', 'VLM.Visuals')
        writer.close()
        dst_stream = dst_gamestg.CreateStream(f'GameItem{n_game_items}', storagecon.STGM_DIRECT | storagecon.STGM_READWRITE | storagecon.STGM_SHARE_EXCLUSIVE | storagecon.STGM_CREATE, 0, 0)
        dst_stream.Write(writer.get_data())
        n_game_items += 1
            
            
    # Mark playfield image has removable
    if next((obj for obj in result_col.all_objects if obj.vlmSettings.bake_type == 'playfield'), None) is not None:
        br = biff_io.BIFF_reader(src_storage.openstream('GameStg/GameData').read())
        while not br.is_eof():
            br.next()
            if br.tag == "IMAG":
                playfield_image = br.get_string()
                #FIXME removed_images[playfield_image]=True
                break
            br.skip_tag()


    # Remove previous packmaps and append the new ones
    n_images = 0
    n_read_images = 0
    while src_storage.exists(f'GameStg/Image{n_read_images}'):
        data = src_storage.openstream(f'GameStg/Image{n_read_images}').read()
        br = biff_io.BIFF_reader(data)
        name = 'unknown'
        while not br.is_eof():
            br.next()
            if br.tag == "NAME":
                name = br.get_string()
                break
            br.skip_tag()
        remove = name.startswith('VLM.')
        remove = remove or (export_mode=='remove_all' and name not in used_images and name in removed_images)
        if remove:
            print(f'. Image {name:>20s} was removed from export table')
        else:
            print(f'. Image {name:>20s} was kept (known users: {used_images.get(name)})')
            dst_stream = dst_gamestg.CreateStream(f'Image{n_images}', storagecon.STGM_DIRECT | storagecon.STGM_READWRITE | storagecon.STGM_SHARE_EXCLUSIVE | storagecon.STGM_CREATE, 0, 0)
            dst_stream.Write(data)
            n_images += 1
        n_read_images = n_read_images + 1

    # Add new bake/lightmap textures
    packmap_index = 0
    while True:
        objects = [obj for obj in result_col.all_objects if obj.vlmSettings.bake_packmap == packmap_index]
        if not objects:
            break
        if export_image_type == 'webp':
            packmap_path = bpy.path.abspath(f"{bakepath}Export/Packmap {packmap_index}.webp")
        elif export_image_type == 'hdr':
            packmap_path = bpy.path.abspath(f"{bakepath}Export/Packmap {packmap_index}.hdr")
        else:
            packmap_path = bpy.path.abspath(f"{bakepath}Export/Packmap {packmap_index}.png")
        img_writer = biff_io.BIFF_writer()
        img_writer.write_tagged_string(b'NAME', f'VLM.Packmap{packmap_index}')
        img_writer.write_tagged_string(b'PATH', packmap_path)
        with open(packmap_path, 'rb') as f:
            img_data = f.read()
            img_writer.write_tagged_u32(b'SIZE', len(img_data))
            img_writer.write_tagged_data(b'DATA', img_data)
        img_writer.close()
        writer = biff_io.BIFF_writer()
        writer.write_tagged_string(b'NAME', f'VLM.Packmap{packmap_index}')
        writer.write_tagged_string(b'PATH', packmap_path)
        writer.write_tagged_u32(b'WDTH', objects[0].vlmSettings.bake_packmap_width)
        writer.write_tagged_u32(b'HGHT', objects[0].vlmSettings.bake_packmap_height)
        writer.write_tagged_empty(b'JPEG') # Strangely, raw data are pushed outside of the JPEG tag (breaking the BIFF structure of the file)
        writer.write_data(img_writer.get_data())
        writer.write_tagged_float(b'ALTV', 1.0)
        writer.close()
        dst_stream = dst_gamestg.CreateStream(f'Image{n_images}', storagecon.STGM_DIRECT | storagecon.STGM_READWRITE | storagecon.STGM_SHARE_EXCLUSIVE | storagecon.STGM_CREATE, 0, 0)
        dst_stream.Write(writer.get_data())
        print(f'. Adding Packmap #{packmap_index} as a {objects[0].vlmSettings.bake_packmap_width:>4} x {objects[0].vlmSettings.bake_packmap_height:>4} image')
        packmap_index += 1
        n_images += 1

    # Copy reference file
    for src_path, mode, hashed in file_structure:
        if not src_storage.exists(src_path):
            continue
        if src_path.startswith('GameStg'):
            dst_st = dst_gamestg
        else:
            dst_st = dst_tableinfo
        data = src_storage.openstream(src_path).read()
        if src_path == 'GameStg/GameData':
            data = bytearray(data)
            br = biff_io.BIFF_reader(data)
            has_solid_bake_mat = has_active_bake_mat = has_light_mat = False
            while not br.is_eof():
                br.next()
                if br.tag == "SIMG": # Number of textures
                    br.put_u32(n_images)
                elif br.tag == "SEDT": # Number of items
                    br.put_u32(n_game_items)
                elif br.tag == "MASI": # Number of materials
                    masi_pos = br.pos
                    n_materials = br.get_u32()
                elif br.tag == "IMAG": # Playfield image
                    playfields = [obj for obj in result_col.all_objects if obj.vlmSettings.bake_type == 'playfield']
                    if playfields:
                        playfield_image = f'VLM.Packmap{playfields[0].vlmSettings.bake_packmap}'
                        if len(playfields) > 1:
                            print(f'. Warning: more than one playfield bake found. Using {playfields[0].name} for the playfield image')
                        wr = biff_io.BIFF_writer()
                        wr.new_tag(b'IMAG')
                        wr.write_string(playfield_image)
                        wr.close(write_endb=False)
                        br.delete_tag()
                        br.insert_data(wr.get_data())
                elif br.tag == "PLMA": # Playfield material
                    wr = biff_io.BIFF_writer()
                    wr.new_tag(b'PLMA')
                    wr.write_string('VLM.Bake.Solid')
                    wr.close(write_endb=False)
                    br.delete_tag()
                    br.insert_data(wr.get_data())
                elif br.tag == "MATE": # Materials
                    mate_pos = br.pos
                    for i in range(n_materials):
                        name = br.get_str(32).rstrip('\x00')
                        if name == 'VLM.Bake.Solid':
                            has_solid_bake_mat = True
                        elif name == 'VLM.Bake.Active':
                            has_active_bake_mat = True
                        elif name == 'VLM.Light':
                            has_light_mat = True
                        br.skip(11 * 4)
                elif br.tag == "PHMA":
                    phma_pos = br.pos
                if br.tag == "CODE":
                    code_pos = br.pos
                    code = br.get_string()
                    br.pos = code_pos
                    br.delete_bytes(len(code) + 4) # Remove the actual len-prepended code string
                    # FIXME find a previously added ZVLM block, load the custom intensity from this block, then replace it
                    code += "\n\n"
                    code += "' ZVLM Begin of Virtual Pinball X Light Mapper generated code\n"
                    code += "Sub VLMTimer_Timer\n"
                    for obj in [obj for obj in result_col.all_objects if obj.vlmSettings.bake_type == 'lightmap']:
                        sync_color = False
                        if obj.vlmSettings.bake_light in light_col.children:
                            baked_lights = light_col.children[obj.vlmSettings.bake_light].objects
                            sync_color = vlm_utils.is_same_light_color(baked_lights, 0.1)
                            vpx_name = baked_lights[0].vlmSettings.vpx_object
                        elif obj.vlmSettings.bake_light in light_col.all_objects:
                            baked_light = context.scene.objects[obj.vlmSettings.bake_light]
                            sync_color = baked_light.type == 'LIGHT'
                            vpx_name = context.scene.objects[obj.vlmSettings.bake_light].vlmSettings.vpx_object
                        def elem_ref(name):
                            name = name[:31] if len(name) > 31 else name
                            if ' ' in name or '.' in name:
                                return f'GetElementByName("{name}")'
                            else:
                                return name
                        if vpx_name in table_lights:
                            code += f'	UpdateLightMapFromLight {elem_ref(vpx_name)}, {elem_ref(export_name(obj.name))}, 100, {"True" if sync_color else "False"}\n'
                        elif vpx_name in table_flashers:
                            code += f'	UpdateLightMapFromFlasher {elem_ref(vpx_name)}, {elem_ref(export_name(obj.name))}, 100\n'
                        else:
                            print(f". {obj.name} is missing a vpx light object to be synchronized on")
                    code += "End Sub\n"
                    code += "\n"
                    code += "Sub UpdateLightMapFromFlasher(flasher, lightmap, intensity_scale)\n"
                    code += "	If flasher.Visible Then\n"
                    code += "		lightmap.Opacity = intensity_scale * flasher.Opacity / 100.0\n"
                    code += "	Else\n"
                    code += "		lightmap.Opacity = 0\n"
                    code += "	End If\n"
                    code += "End Sub\n"
                    code += "\n"
                    code += "Sub UpdateLightMapFromLight(light, lightmap, intensity_scale, sync_color)\n"
                    code += "	Dim percent: percent = light.GetCurrentIntensity() / light.Intensity\n"
                    code += "	lightmap.Opacity = intensity_scale * percent\n"
                    code += "	If sync_color Then lightmap.Color = light.colorfull\n"
                    code += "End Sub\n"
                    code += "' ZVLM End of Virtual Pinball X Light Mapper generated code\n"
                    wr = biff_io.BIFF_writer()
                    wr.write_string(code)
                    br.insert_data(wr.get_data())
                else:
                    br.skip_tag()
            # modify existing data to add missing VLM materials
            n_material_to_add = 0
            wr = biff_io.BIFF_writer()
            pr = biff_io.BIFF_writer()
            if not has_solid_bake_mat:
                n_material_to_add += 1
                wr.write_data(b'VLM.Bake.Solid\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00')
                pr.write_data(b'VLM.Bake.Solid\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00')
                wr.write_u32(0xFFFFFF) # Base color
                wr.write_u32(0x000000) # Glossy color
                wr.write_u32(0x000000) # Clearcoat color
                wr.write_float(0.0) # Wrap lighting
                wr.write_bool(False) # Metal
                wr.write_float(0.0) # Shininess
                wr.write_u32(0) # Glossy image lerp
                wr.write_float(0.0) # Edge
                wr.write_u32(0x0c) # Thickness
                wr.write_float(1.0) # Opacity
                wr.write_u32(0x000000FE) # Active & edge alpha
                pr.write_float(0.0)
                pr.write_float(0.0)
                pr.write_float(0.0)
                pr.write_float(0.0)
            if not has_active_bake_mat:
                n_material_to_add += 1
                wr.write_data(b'VLM.Bake.Active\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00')
                pr.write_data(b'VLM.Bake.Active\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00')
                wr.write_u32(0xFFFFFF) # Base color
                wr.write_u32(0x000000) # Glossy color
                wr.write_u32(0x000000) # Clearcoat color
                wr.write_float(0.0) # Wrap lighting
                wr.write_bool(False) # Metal
                wr.write_float(0.0) # Shininess
                wr.write_u32(0) # Glossy image lerp
                wr.write_float(0.0) # Edge
                wr.write_u32(0x0c) # Thickness
                wr.write_float(1.0) # Opacity
                wr.write_u32(0x000000FF) # Active & edge alpha
                pr.write_float(0.0)
                pr.write_float(0.0)
                pr.write_float(0.0)
                pr.write_float(0.0)
            if not has_light_mat:
                n_material_to_add += 1
                wr.write_data(b'VLM.Lightmap\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00')
                pr.write_data(b'VLM.Lightmap\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00')
                wr.write_u32(0xFFFFFF) # Base color
                wr.write_u32(0x000000) # Glossy color
                wr.write_u32(0x000000) # Clearcoat color
                wr.write_float(0.0) # Wrap lighting
                wr.write_bool(False) # Metal
                wr.write_float(0.0) # Shininess
                wr.write_u32(0) # Glossy image lerp
                wr.write_float(0.0) # Edge
                wr.write_u32(0x0c) # Thickness
                wr.write_float(1.0) # Opacity
                wr.write_u32(0x000000FF) # Active & edge alpha
                pr.write_float(0.0)
                pr.write_float(0.0)
                pr.write_float(0.0)
                pr.write_float(0.0)
            print(f'. Adding {n_material_to_add} materials')
            br.pos = masi_pos
            br.put_u32(n_materials + n_material_to_add)
            br.pos = mate_pos - 8
            br.put_u32((n_materials + n_material_to_add) * 76 + 4)
            for i, d in enumerate(wr.get_data()):
                br.data.insert(mate_pos + i, d)
            if phma_pos > mate_pos:
                phma_pos += len(wr.get_data())
            br.pos = phma_pos - 8
            br.put_u32((n_materials + n_material_to_add) * 48 + 4)
            for i, d in enumerate(pr.get_data()):
                br.data.insert(phma_pos + i, d)
            data = bytes(br.data)
        if hashed:
            if mode == 0:
                data_hash.CryptHashData(data)
            elif mode == 1:
                br = biff_io.BIFF_reader(data)
                while not br.is_eof():
                    br.next()
                    if br.tag == "CODE": # For some reason, the code length info is not hashed, just the tag and code string
                        data_hash.CryptHashData(b'CODE')
                        code_length = br.get_u32() 
                        data_hash.CryptHashData(br.get(code_length))
                    else: # Biff tags and data are hashed but not their size
                        data_hash.CryptHashData(br.get_record_data(True))
        dst_stream = dst_st.CreateStream(src_path.split('/')[-1], storagecon.STGM_DIRECT | storagecon.STGM_READWRITE | storagecon.STGM_SHARE_EXCLUSIVE | storagecon.STGM_CREATE, 0, 0)
        dst_stream.Write(data)
        if src_path == 'GameStg/CustomInfoTags': # process the custom info tags since they need to be hashed
            br = biff_io.BIFF_reader(data)
            while not br.is_eof():
                br.next()
                if br.tag == "CUST":
                    cust_name = br.get_string()
                    print(f'Hashing custom information block {cust_name}')
                    if src_storage.exists(f'TableInfo/f{cust_name}'):
                        data = src_storage.openstream(f'TableInfo/f{cust_name}').read()
                        data_hash.CryptHashData(data)
                        dst_stream = dst_tableinfo.CreateStream(cust_name, storagecon.STGM_DIRECT | storagecon.STGM_READWRITE | storagecon.STGM_SHARE_EXCLUSIVE | storagecon.STGM_CREATE, 0, 0)
                        dst_stream.Write(data)
                else:
                    br.skip_tag()

    print(f". {n_images} images exported in table files")
    print(". Images marked as used:", list(used_images.keys()))
    print(". Images marked as deletable:", list(removed_images.keys()))

    hash_size = data_hash.CryptGetHashParam(win32cryptcon.HP_HASHSIZE)
    file_hash = data_hash.CryptGetHashParam(win32cryptcon.HP_HASHVAL)
    data_hash.CryptDestroyHash()
    crypt_context.CryptReleaseContext()
    dst_stream = dst_gamestg.CreateStream('MAC', storagecon.STGM_DIRECT | storagecon.STGM_READWRITE | storagecon.STGM_SHARE_EXCLUSIVE | storagecon.STGM_CREATE, 0, 0)
    dst_stream.Write(file_hash)
    dst_storage.Commit(storagecon.STGC_DEFAULT)
    src_storage.close()
    
    print(f"\nExport finished.")
    return {"FINISHED"}
    