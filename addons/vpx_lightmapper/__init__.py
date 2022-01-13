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

bl_info = {
    "name": "Visual Pinball X Light Mapper",
    "author": "Vincent Bousquet",
    "version": (0, 0, 1),
    "blender": (3, 0, 0),
    "description": "Import/Export Visual Pinball X tables and perform automated light baking",
    "warning": "Requires installation of dependencies",
    "wiki_url": "",
    "tracker_url": "",
    "support": "COMMUNITY",
    "category": "Import-Export"}

import bpy
import os
import sys
import importlib
import math
import mathutils
from bpy_extras.io_utils import (ImportHelper, axis_conversion)
from bpy.props import (StringProperty, BoolProperty, IntProperty, FloatProperty, FloatVectorProperty, EnumProperty, PointerProperty)
from bpy.types import (Panel, Menu, Operator, PropertyGroup, AddonPreferences, Collection)
from rna_prop_ui import PropertyPanel

# TODO
# - Move bake results properties to object properties and provide adequate UI



# Use import.reload for all submodule to allow iterative development using bpy.ops.script.reload()
if "vlm_dependencies" in locals():
    importlib.reload(vlm_dependencies)
else:
    from . import vlm_dependencies
if "vlm_collections" in locals():
    importlib.reload(vlm_collections)
else:
    from . import vlm_collections
if "vlm_utils" in locals():
    importlib.reload(vlm_utils)
else:
    from . import vlm_utils
if "vlm_uvpacker" in locals():
    importlib.reload(vlm_uvpacker)
else:
    from . import vlm_uvpacker

# Only load submodules that have external dependencies if they are satisfied
dependencies = (
    # OLE lib: https://olefile.readthedocs.io/en/latest/Howto.html
    vlm_dependencies.Dependency(module="olefile", package=None, name=None),
    vlm_dependencies.Dependency(module="PIL", package=None, name="Pillow"),
)
dependencies_installed = vlm_dependencies.import_dependencies(dependencies)
if dependencies_installed:
    if "vlm_import" in locals():
        importlib.reload(vlm_import)
    else:
        from . import vlm_import
    if "vlm_export" in locals():
        importlib.reload(vlm_export)
    else:
        from . import vlm_export
    if "vlm_baker" in locals():
        importlib.reload(vlm_baker)
    else:
        from . import vlm_baker


class VLM_Scene_props(PropertyGroup):
    # Importer
    light_size: FloatProperty(name="Light Size", description="Light size factor from VPX to Blender", default = 5.0)
    light_intensity: FloatProperty(name="Light Intensity", description="Light intensity factor from VPX to Blender", default = 25.0)
    table_file: StringProperty(name="Table", description="Table filename", default="")
    playfield_size: FloatVectorProperty(name="Playfield size:", description="Size of the playfield in VP unit", default=(0, 0, 0, 0), size=4)
    # Baker
    tex_size: IntProperty(name="Tex Size:", description="Texture size", default = 256, min = 8)
    padding: IntProperty(name="Padding:", description="Padding between bakes", default = 2, min = 0)
    remove_backface: FloatProperty(name="Backface Limit", description="Angle (degree) limit for backfacing geometry removal", default = 0.0)
    export_on_bake: BoolProperty(name="Export after bake", description="Export all meshes and packmaps after baking", default = False)
    # Exporter
    export_webp: BoolProperty(name="Export WebP", description="Additionally to the PNG, export WebP", default = False)


class VLM_Collection_props(PropertyGroup):
    bake_mode: EnumProperty(
        items=[
            ('default', 'Default', 'Default bake process', '', 0),
            ('movable', 'Movable', 'Bake to a splitted movable mesh', '', 1),
            ('playfield', 'Playfield', 'Bake to a dedicated orthographic playfield image', '', 2)
        ],
        default='default'
    )
    light_mode: BoolProperty(name="Group lights", description="Bake all lights as a group", default = True)


class VLM_Object_props(PropertyGroup):
    import_mesh: BoolProperty(name="Mesh", description="Update mesh on import", default = True)
    import_transform: BoolProperty(name="Transform", description="Update transform on import", default = True)


class VLM_OT_new(Operator):
    bl_idname = "vlm.new_operator"
    bl_label = "New"
    bl_description = "Start a new empty project"
    bl_options = {"REGISTER", "UNDO"}
    
    def execute(self, context):
        context.scene.render.engine = 'CYCLES'
        context.scene.cycles.samples = 64
        context.scene.render.film_transparent = True
        context.scene.cycles.use_preview_denoising = True
        context.scene.vlmSettings.table_file = ""
        vlm_collections.delete_collection(vlm_collections.get_collection('ROOT'))
        vlm_collections.setup_collections()
        return {'FINISHED'}


class VLM_OT_new_from_vpx(Operator, ImportHelper):
    bl_idname = "vlm.new_from_vpx_operator"
    bl_label = "New from VPX"
    bl_description = "Start a new VPX lightmap project"
    bl_options = {"REGISTER", "UNDO"}
    filename_ext = ".vpx"
    filter_glob: StringProperty(default="*.vpx", options={'HIDDEN'}, maxlen=255,)
    
    def execute(self, context):
        context.scene.render.engine = 'CYCLES'
        context.scene.cycles.samples = 64
        context.scene.render.film_transparent = True
        context.scene.cycles.use_preview_denoising = True
        context.scene.vlmSettings.table_file = ""
        vlm_collections.delete_collection(vlm_collections.get_collection('ROOT'))
        return vlm_import.read_vpx(context, self.filepath)


class VLM_OT_update(Operator):
    bl_idname = "vlm.update_operator"
    bl_label = "Update"
    bl_description = "Update this project from the VPX file"
    bl_options = {"REGISTER", "UNDO"}
    
    @classmethod
    def poll(cls, context):
        return os.path.exists(bpy.path.abspath(context.scene.vlmSettings.table_file))

    def execute(self, context):
        vlmProps = context.scene.vlmSettings
        return vlm_import.read_vpx(context, bpy.path.abspath(context.scene.vlmSettings.table_file))


class VLM_OT_texsize(Operator):
    bl_idname = "vlm.tex_size_operator"
    bl_label = "Size"
    bl_description = "Texture size"
    size: bpy.props.IntProperty()

    def execute(self, context):
        context.scene.vlmSettings.tex_size = self.size
        return {"FINISHED"}


class VLM_OT_bake_all(Operator):
    bl_idname = "vlm.bake_all_operator"
    bl_label = "Bake All"
    bl_description = "Bake each object for each light groups (lengthy operation)"
    bl_options = {"REGISTER", "UNDO"}
    
    def execute(self, context):
        vlmProps = context.scene.vlmSettings

        # Full bake
        vlm_baker.full_bake(context)
    
        # Eventually export the result, baking the final packmap
        opt_export_on_bake = vlmProps.export_on_bake # True if we want the packmap to be rendered
        opt_save_webp = vlmProps.export_webp # Additionally convert the exported pack map to webp (keeping the default png as well)
        if opt_export_on_bake:
            bakepath = f"//{bpy.path.basename(context.blend_data.filepath)} - Bakes/"
            for obj in bake_results:
                export_packmap(obj, obj["vlm.name"], obj["vlm.is_light"] != 0, obj["vlm.tex_width"], obj["vlm.tex_height"], opt_save_webp, vlmProps.padding, False, f"{bakepath}{obj['vlm.name']}.png")

        return {"FINISHED"}


class VLM_OT_state_hide(Operator):
    bl_idname = "vlm.state_hide_operator"
    bl_label = "Hide"
    bl_description = "Hide object from bake"
    bl_options = {"REGISTER", "UNDO"}
    
    @classmethod
    def poll(cls, context):
        root_col = vlm_collections.get_collection('ROOT', create=False)
        target_col = vlm_collections.get_collection('HIDDEN', create=False)
        return root_col is not None and target_col is not None and \
            next((o for o in context.selected_objects if o.name in root_col.all_objects and o.name not in target_col.all_objects), None) is not None

    def execute(self, context):
        root_col = vlm_collections.get_collection('ROOT', create=False)
        target_col = vlm_collections.get_collection('HIDDEN', create=False)
        if root_col is not None and target_col is not None:
            for obj in [obj for obj in context.selected_objects if obj.name in root_col.all_objects and obj.name not in target_col.all_objects]:
                target_col.objects.link(obj)
                [col.objects.unlink(obj) for col in obj.users_collection if col != target_col]
        return {"FINISHED"}


class VLM_OT_state_indirect(Operator):
    bl_idname = "vlm.state_indirect_operator"
    bl_label = "Indirect"
    bl_description = "Hide object from bake, but keep indirect interaction"
    bl_options = {"REGISTER", "UNDO"}
    
    @classmethod
    def poll(cls, context):
        root_col = vlm_collections.get_collection('ROOT', create=False)
        target_col = vlm_collections.get_collection('INDIRECT', create=False)
        return root_col is not None and target_col is not None and \
            next((o for o in context.selected_objects if o.name in root_col.all_objects and o.name not in target_col.all_objects), None) is not None

    def execute(self, context):
        root_col = vlm_collections.get_collection('ROOT', create=False)
        target_col = vlm_collections.get_collection('INDIRECT', create=False)
        if root_col is not None and target_col is not None:
            for obj in [obj for obj in context.selected_objects if obj.name in root_col.all_objects and obj.name not in target_col.all_objects]:
                target_col.objects.link(obj)
                [col.objects.unlink(obj) for col in obj.users_collection if col != target_col]
        return {"FINISHED"}


class VLM_OT_state_bake(Operator):
    bl_idname = "vlm.state_bake_operator"
    bl_label = "Bake"
    bl_description = "Enable objects for baking"
    bl_options = {"REGISTER", "UNDO"}
    
    @classmethod
    def poll(cls, context):
        root_col = vlm_collections.get_collection('ROOT', create=False)
        target_col = vlm_collections.get_collection('BAKE', create=False)
        return root_col is not None and target_col is not None and \
            next((o for o in context.selected_objects if o.name in root_col.all_objects and o.name not in target_col.all_objects and o.type != 'LIGHT'), None) is not None
        return False

    def execute(self, context):
        root_col = vlm_collections.get_collection('ROOT', create=False)
        target_col = vlm_collections.get_collection('BAKE DEFAULT', create=False)
        if root_col is not None and target_col is not None:
            for obj in [obj for obj in context.selected_objects if obj.name in root_col.all_objects and obj.name not in target_col.all_objects and obj.type != 'LIGHT']:
                target_col.objects.link(obj)
                [col.objects.unlink(obj) for col in obj.users_collection if col != target_col]
        return {"FINISHED"}


class VLM_OT_state_import_mesh(Operator):
    bl_idname = "vlm.state_import_mesh"
    bl_label = "Mesh"
    bl_description = "Update mesh on import"
    bl_options = {"REGISTER", "UNDO"}
    enable_import: bpy.props.BoolProperty()
    
    @classmethod
    def poll(cls, context):
        bake_col = vlm_collections.get_collection('ROOT', create=False)
        return bake_col is not None and next((obj for obj in context.selected_objects if obj.name in bake_col.all_objects), None) is not None

    def execute(self, context):
        bake_col = vlm_collections.get_collection('ROOT', create=False)
        if bake_col is not None:
            for obj in [obj for obj in context.selected_objects if obj.name in bake_col.all_objects]:
                obj.vlmSettings.import_mesh = self.enable_import
        return {"FINISHED"}


class VLM_OT_state_import_material(Operator):
    bl_idname = "vlm.state_import_material"
    bl_label = "Material"
    bl_description = "Update material on import"
    bl_options = {"REGISTER", "UNDO"}
    enable_material: bpy.props.BoolProperty()
    
    @classmethod
    def poll(cls, context):
        bake_col = vlm_collections.get_collection('ROOT', create=False)
        return bake_col is not None and next((obj for obj in context.selected_objects if obj.name in bake_col.all_objects), None) is not None

    def execute(self, context):
        bake_col = vlm_collections.get_collection('ROOT', create=False)
        if bake_col is not None:
            for obj in [obj for obj in context.selected_objects if obj.name in bake_col.all_objects]:
                obj.vlmSettings.import_material = self.enable_material
        return {"FINISHED"}


class VLM_OT_state_import_transform(Operator):
    bl_idname = "vlm.state_import_transform"
    bl_label = "Transform"
    bl_description = "Update transform on import"
    bl_options = {"REGISTER", "UNDO"}
    enable_transform: bpy.props.BoolProperty()
    
    @classmethod
    def poll(cls, context):
        bake_col = vlm_collections.get_collection('ROOT', create=False)
        return bake_col is not None and next((obj for obj in context.selected_objects if obj.name in bake_col.all_objects), None) is not None

    def execute(self, context):
        bake_col = vlm_collections.get_collection('ROOT', create=False)
        if bake_col is not None:
            for obj in [obj for obj in context.selected_objects if obj.name in bake_col.all_objects]:
                obj.vlmSettings.import_transform = self.enable_transform
        return {"FINISHED"}


class VLM_OT_export_packmap(Operator):
    bl_idname = "vlm.export_packmap_operator"
    bl_label = "Bake PackMap"
    bl_description = "Compute and save the packed bake map for the selected bake meshes"
    bl_options = {"REGISTER"}
    
    @classmethod
    def poll(cls, context):
        object_col = vlm_collections.get_collection('BAKE RESULT', create=False)
        if object_col is not None:
            for obj in context.selected_objects:
                if obj.name in object_col.all_objects and "vlm.name" in obj and "vlm.is_light" in obj and "vlm.tex_width" in obj and "vlm.tex_height" in obj:
                    return True
        return False

    def execute(self, context):
        vlmProps = context.scene.vlmSettings
        result_col = vlm_collections.get_collection('BAKE RESULT')
        bakepath = f"//{bpy.path.basename(context.blend_data.filepath)} - Bakes/"
        for obj in context.selected_objects:
            if obj.name in result_col.all_objects:
                vlm_export.export_packmap(obj, obj["vlm.name"], obj["vlm.is_light"] != 0, obj["vlm.tex_width"], obj["vlm.tex_height"], vlmProps.export_webp, vlmProps.padding, False, f"{bakepath}{obj['vlm.name']}.png")
        return {"FINISHED"}


class VLM_OT_export_all(Operator):
    bl_idname = "vlm.export_all_operator"
    bl_label = "Export All"
    bl_description = "Compute packmaps and export all baked models"
    bl_options = {"REGISTER"}
    
    def execute(self, context):
        vlmProps = context.scene.vlmSettings
        return vlm_export.export_all(context)


class VLM_PT_Properties(bpy.types.Panel):
    bl_label = "Visual Pinball X Light Mapper"
    bl_category = "VLM"
    bl_space_type = 'PROPERTIES'
    bl_region_type = 'WINDOW'
    bl_context = "scene"

    def draw(self, context):
        layout = self.layout
        scene = context.scene
        vlmProps = scene.vlmSettings

        layout.label(text="VPX Importer", icon='IMPORT') 
        row = layout.row()
        row.scale_y = 1.5
        row.operator(VLM_OT_new.bl_idname)
        row.operator(VLM_OT_new_from_vpx.bl_idname)
        row.operator(VLM_OT_update.bl_idname)
        row = layout.row()
        row.prop(vlmProps, "light_size")
        row.prop(vlmProps, "light_intensity")
        layout.label(text=vlmProps.table_file)

        layout.separator()

        layout.label(text="LightMap Baker", icon='RENDERLAYERS') 
        row = layout.row(align=True)
        row.scale_y = 1.5
        row.operator(VLM_OT_texsize.bl_idname, text="256").size = 256
        row.operator(VLM_OT_texsize.bl_idname, text="512").size = 512
        row.operator(VLM_OT_texsize.bl_idname, text="1k").size = 1024
        row.operator(VLM_OT_texsize.bl_idname, text="2k").size = 2048
        row.operator(VLM_OT_texsize.bl_idname, text="4k").size = 4096
        row.operator(VLM_OT_texsize.bl_idname, text="8k").size = 8192
        row = layout.row()
        row.prop(vlmProps, "tex_size")
        row.prop(vlmProps, "padding")
        row = layout.row()
        row.prop(vlmProps, "remove_backface")
        row.prop(vlmProps, "export_on_bake")
        row = layout.row()
        row.scale_y = 1.5
        row.operator(VLM_OT_bake_all.bl_idname)
        
        layout.separator()

        layout.label(text="Baked Model Exporter", icon='EXPORT') 
        row = layout.row()
        row.prop(vlmProps, "export_webp")
        row = layout.row()
        row.scale_y = 1.5
        row.operator(VLM_OT_export_all.bl_idname)
        
        layout.separator()

        versionStr = "VPX Light Mapper Version: %d.%d.%d" % bl_info["version"]
        layout.label(text=versionStr, icon="SETTINGS") 
        row = layout.row()
        row.scale_y = 1.5
        row.operator("wm.url_open", text="Light Mapper" , icon="QUESTION").url = "https://github.com/vbousquet/vlm"
        row.operator("wm.url_open", text="Visual Pinball X", icon="HOME").url = "https://github.com/vpinball/vpinball"


class VLM_PT_Col_Props(bpy.types.Panel):
    bl_label = "Visual Pinball X Light Mapper"
    bl_category = "VLM"
    bl_space_type = 'PROPERTIES'
    bl_region_type = 'WINDOW'
    bl_context = "collection"

    def draw(self, context):
        layout = self.layout
        col = context.collection
        bake_col = vlm_collections.get_collection('BAKE')
        light_col = vlm_collections.get_collection('LIGHTS')
        if col.name in bake_col.children:
            layout.label(text="Bake mode:") 
            layout.prop(col.vlmSettings, 'bake_mode', expand=True)
        elif col.name in light_col.children:
            layout.prop(col.vlmSettings, 'light_mode', expand=True)
        else:
            layout.label(text="Select a bake or light group") 


class VLM_PT_3D(bpy.types.Panel):
    bl_label = "Visual Pinball X Light Mapper"
    bl_category = "VLM"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    def draw(self, context):
        layout = self.layout
        show_info = True

        root_col = vlm_collections.get_collection('ROOT', create=False)
        if root_col is not None:
            bake_objects = [obj for obj in context.selected_objects if obj.name in root_col.all_objects]
            if bake_objects:
                show_info = False
                layout.label(text="Import options:")
                row = layout.row(align=True)
                row.scale_y = 1.5
                if all((x.vlmSettings.import_mesh for x in bake_objects)):
                    row.operator(VLM_OT_state_import_mesh.bl_idname, text='On', icon='MESH_DATA').enable_import = False
                elif all((not x.vlmSettings.import_mesh for x in bake_objects)):
                    row.operator(VLM_OT_state_import_mesh.bl_idname, text='Off', icon='MESH_DATA').enable_import = True
                else:
                    row.operator(VLM_OT_state_import_mesh.bl_idname, text='-', icon='MESH_DATA').enable_import = True
                if all((x.vlmSettings.import_transform for x in bake_objects)):
                    row.operator(VLM_OT_state_import_transform.bl_idname, text='On', icon='OBJECT_ORIGIN').enable_transform = False
                elif all((not x.vlmSettings.import_transform for x in bake_objects)):
                    row.operator(VLM_OT_state_import_transform.bl_idname, text='Off', icon='OBJECT_ORIGIN').enable_transform = True
                else:
                    row.operator(VLM_OT_state_import_transform.bl_idname, text='-', icon='MATERIAL').enable_transform = True
                layout.separator()
                layout.label(text="Bake visibility:")
                row = layout.row(align=True)
                row.scale_y = 1.5
                row.operator(VLM_OT_state_hide.bl_idname)
                row.operator(VLM_OT_state_indirect.bl_idname)
                row.operator(VLM_OT_state_bake.bl_idname)

        result_col = vlm_collections.get_collection('BAKE RESULT', create=False)
        if result_col is not None and next((x for x in context.selected_objects if x.name in result_col.all_objects), None) is not None:
            show_info = False
            layout.separator()
            layout.operator(VLM_OT_export_packmap.bl_idname)

        if show_info:
            layout.label(text="Select a baked object or a bake result") 


class VLM_PT_3D_warning_panel(bpy.types.Panel):
    bl_label = "Visual Pinball X Light Mapper"
    bl_category = "VLM"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"

    @classmethod
    def poll(self, context):
        return not dependencies_installed

    def draw(self, context):
        layout = self.layout
        lines = [f"Please install the missing dependencies",
                 f"for the \"{bl_info.get('name')}\" add-on.",
                 f"1. Open the preferences (Edit > Preferences > Add-ons).",
                 f"2. Search for the \"{bl_info.get('name')}\" add-on.",
                 f"3. Open the details section of the add-on.",
                 f"4. Click on the \"{VLM_OT_install_dependencies.bl_label}\" button.",
                 f"   This will download and install the missing",
                 f"   Python packages, if Blender has the required",
                 f"   permissions."]
        for line in lines:
            layout.label(text=line)


class VLM_PT_Props_warning_panel(bpy.types.Panel):
    bl_label = "Visual Pinball X Light Mapper"
    bl_category = "VLM"
    bl_space_type = 'PROPERTIES'
    bl_region_type = 'WINDOW'

    @classmethod
    def poll(self, context):
        return not dependencies_installed

    def draw(self, context):
        layout = self.layout
        lines = [f"Please install the missing dependencies",
                 f"for the \"{bl_info.get('name')}\" add-on.",
                 f"1. Open the preferences (Edit > Preferences > Add-ons).",
                 f"2. Search for the \"{bl_info.get('name')}\" add-on.",
                 f"3. Open the details section of the add-on.",
                 f"4. Click on the \"{VLM_OT_install_dependencies.bl_label}\" button.",
                 f"   This will download and install the missing",
                 f"   Python packages, if Blender has the required",
                 f"   permissions."]
        for line in lines:
            layout.label(text=line)


class VLM_OT_install_dependencies(bpy.types.Operator):
    bl_idname = "vlm.install_dependencies"
    bl_label = "Install dependencies"
    bl_description = ("Downloads and installs the required python packages for this add-on. "
                      "Internet connection is required. Blender may have to be started with "
                      "elevated permissions in order to install the package")
    bl_options = {"REGISTER", "INTERNAL"}

    @classmethod
    def poll(self, context):
        return not dependencies_installed

    def execute(self, context):
        try:
            vlm_dependencies.install_dependencies(dependencies)
        except (subprocess.CalledProcessError, ImportError) as err:
            self.report({"ERROR"}, str(err))
            return {"CANCELLED"}
        global dependencies_installed
        dependencies_installed = True
        for cls in classes:
            bpy.utils.register_class(cls)
        return {"FINISHED"}


class VLM_preferences(bpy.types.AddonPreferences):
    bl_idname = __name__

    def draw(self, context):
        layout = self.layout
        layout.operator(VLM_OT_install_dependencies.bl_idname, icon="CONSOLE")


classes = (
    VLM_Scene_props,
    VLM_Collection_props,
    VLM_Object_props,
    VLM_PT_Col_Props,
    VLM_PT_3D,
    VLM_PT_Properties,
    VLM_OT_new,
    VLM_OT_new_from_vpx,
    VLM_OT_update,
    VLM_OT_texsize,
    VLM_OT_bake_all,
    VLM_OT_state_hide,
    VLM_OT_state_indirect,
    VLM_OT_state_bake,
    VLM_OT_state_import_mesh,
    VLM_OT_state_import_transform,
    VLM_OT_export_packmap,
    VLM_OT_export_all,
    )
preference_classes = (VLM_PT_3D_warning_panel, VLM_PT_Props_warning_panel, VLM_OT_install_dependencies, VLM_preferences)
registered_classes = []


def register():
    global dependencies_installed
    dependencies_installed = False
    for cls in preference_classes:
        bpy.utils.register_class(cls)
        registered_classes.append(cls)
    dependencies_installed = vlm_dependencies.import_dependencies(dependencies)
    if dependencies_installed:
        for cls in classes:
            bpy.utils.register_class(cls)
            registered_classes.append(cls)
        bpy.types.Scene.vlmSettings = PointerProperty(type=VLM_Scene_props)
        bpy.types.Collection.vlmSettings = PointerProperty(type=VLM_Collection_props)
        bpy.types.Object.vlmSettings = PointerProperty(type=VLM_Object_props)
    else:
        print(f"VPX light mapper was not installed due to missing dependencies")


def unregister():
    for cls in registered_classes:
        bpy.utils.unregister_class(cls)
    if dependencies_installed:
        del bpy.types.Scene.vlmSettings
        del bpy.types.Collection.vlmSettings
        del bpy.types.Object.vlmSettings


if __name__ == "__main__":
    register()