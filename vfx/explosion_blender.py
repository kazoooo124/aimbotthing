#!/usr/bin/env python3
"""explosion_blender.py - a missile streaks in and hits: cinematic explosion made with Blender.

A REAL fluid simulation (Blender's Mantaflow: fire + smoke), not a texture or a looping video:
  * missile with a hot exhaust + smoke trail, then the impact fireball and billowing smoke
  * dust shockwave rolling along the ground, sparks and debris with motion blur
  * hazy backlit dusk: the ruined skyline is silhouetted against the glow
  * camera that pushes in as the blast grows

Two stages (so the slow physics only runs once, and you can re-render without re-simulating):
    --stage sim      the fire/smoke physics -> VDB files. Needs a Blender whose Mantaflow works.
    --stage render   loads those VDB files and renders the picture.
    --stage all      both, in one go (default)

Run it either way:
    blender -b -P explosion_blender.py -- --out render_dir               (a normal Blender install)
    pip install bpy numpy ; python explosion_blender.py --out render_dir (Blender as a Python module;
                                                                           its fluid sim can be broken
                                                                           - then use --stage render only)
Then:  python explosion_post.py render_dir -o explosion.mp4

Slow? This is CPU path-tracing of fire. A GPU helps hugely:  --gpu
Quality knobs:  --res (sim detail)  --noise  --width/--height  --samples  --frames
"""
import argparse
import json
import math
import os
import random
import sys
import time

import bpy
from mathutils import Euler, Vector

FPS = 24
T_IMPACT = 24                              # frame the missile hits and the blast starts
DOMAIN = (120.0, 120.0, 150.0)             # simulation box in metres (x, y, z); ground level is z = 0
IMPACT = Vector((0.0, 0.0, 4.0))
CAM_AZIMUTH = math.radians(-135)           # where the camera sits around the blast
VIEW = Vector((-math.cos(CAM_AZIMUTH), -math.sin(CAM_AZIMUTH), 0.0))   # camera looks this way
RIGHT = Vector((VIEW.y, -VIEW.x, 0.0))     # screen-right
MISSILE_START = IMPACT + RIGHT * 58 + Vector((0, 0, 50)) - VIEW * 12
MISSILE_DIR = (IMPACT - MISSILE_START).normalized()
NOZZLE_OFFSET = 3.4                        # exhaust sits this far behind the missile's centre
SUN_ROTATION = math.radians(58)            # sun sits low, in front of the camera = backlit skyline


def get_args():
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else sys.argv[1:]
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--out", default="render_out", help="folder for the sim cache and the PNG frames")
    p.add_argument("--frames", type=int, default=96, help="number of frames (24 fps)")
    p.add_argument("--res", type=int, default=128, help="fluid sim resolution (64 fast/blocky ... 256 detailed/slow)")
    p.add_argument("--noise", type=int, default=2, help="extra detail factor on top of --res (0 = off)")
    p.add_argument("--width", type=int, default=1280)
    p.add_argument("--height", type=int, default=720)
    p.add_argument("--samples", type=int, default=32, help="render samples per pixel")
    p.add_argument("--step", type=float, default=1.5, help="volume step rate: higher = faster, coarser")
    p.add_argument("--haze", action="store_true", help="atmospheric haze for depth (nicer, slower)")
    p.add_argument("--only", help="render only these frames, e.g. 1,12,30 or 10-20")
    p.add_argument("--stage", choices=["all", "sim", "render"], default="all")
    p.add_argument("--gpu", action="store_true", help="render on the GPU if there is one")
    p.add_argument("--seed", type=int, default=3)
    return p.parse_args(argv)


def log(*a):
    print("[explosion]", *a, flush=True)


# ------------------------------------------------------------------ helpers

def mat_node_tree(name):
    m = bpy.data.materials.new(name)
    m.use_nodes = True
    nt = m.node_tree
    nt.nodes.clear()
    return m, nt


def principled_mat(name, color, rough=0.9, emission=None, strength=0.0):
    m, nt = mat_node_tree(name)
    out = nt.nodes.new("ShaderNodeOutputMaterial")
    bsdf = nt.nodes.new("ShaderNodeBsdfPrincipled")
    bsdf.inputs["Base Color"].default_value = (*color, 1)
    bsdf.inputs["Roughness"].default_value = rough
    if emission:
        bsdf.inputs["Emission Color"].default_value = (*emission, 1)
        bsdf.inputs["Emission Strength"].default_value = strength
    nt.links.new(bsdf.outputs[0], out.inputs["Surface"])
    return m


def set_if(obj, **kw):
    for k, v in kw.items():
        try:
            setattr(obj, k, v)
        except (AttributeError, TypeError, ValueError):
            pass


def interpolation(kind):
    try:
        bpy.context.preferences.edit.keyframe_new_interpolation_type = kind
    except (AttributeError, TypeError):
        pass


def nozzle_at(frame):
    """Where the missile's exhaust is on a given frame (linear flight from start to impact)."""
    k = max(0.0, min(1.0, (frame - 1) / (T_IMPACT - 1)))
    centre = MISSILE_START.lerp(IMPACT, k)
    return centre - MISSILE_DIR * NOZZLE_OFFSET


# ------------------------------------------------------------------ SIM STAGE

def make_sim_scene(a):
    """Only the fluid domain and the things that emit fire/smoke - nothing to render."""
    interpolation("LINEAR")
    bpy.ops.mesh.primitive_cube_add(size=1, location=(0, 0, DOMAIN[2] / 2))
    dom = bpy.context.object
    dom.name = "Domain"
    dom.scale = DOMAIN
    mod = dom.modifiers.new("Fluid", "FLUID")
    mod.fluid_type = "DOMAIN"
    ds = mod.domain_settings
    ds.domain_type = "GAS"
    ds.resolution_max = a.res
    ds.use_adaptive_domain = False           # grid always covers the whole box -> easy to place later
    ds.use_noise = a.noise > 0
    if a.noise > 0:
        set_if(ds, noise_scale=a.noise, noise_strength=1.0, noise_pos_scale=2.0, noise_time_anim=0.4)
    ds.cache_type = "REPLAY"                 # simulate as the timeline advances (works headless)
    ds.cache_directory = os.path.join(os.path.abspath(a.out), "fluid")
    ds.cache_data_format = "OPENVDB"
    set_if(ds, cache_noise_format="OPENVDB")
    ds.cache_frame_start, ds.cache_frame_end = 1, a.frames
    ds.alpha = 1.1            # smoke buoyancy
    ds.beta = 2.4             # heat -> rises
    ds.vorticity = 0.6        # swirl => billowing cauliflower
    ds.burning_rate = 0.36    # lower = the fireball burns longer
    ds.flame_smoke = 1.0
    ds.flame_vorticity = 1.0
    ds.flame_ignition = 1.4
    ds.flame_max_temp = 3.8
    ds.use_dissolve_smoke = True
    ds.dissolve_speed = 320
    ds.use_dissolve_smoke_log = True
    ds.use_collision_border_bottom = True
    for side in ("left", "right", "front", "back", "top"):
        setattr(ds, f"use_collision_border_{side}", False)
    set_if(ds, cfl_condition=2.0, timesteps_max=4)

    def flow_object(obj, flow_type, **kw):
        fm = obj.modifiers.new("Fluid", "FLUID")
        fm.fluid_type = "FLOW"
        fs = fm.flow_settings
        fs.flow_type = flow_type
        fs.flow_behavior = "INFLOW"
        fs.flow_source = "MESH"
        for k, v in kw.items():
            setattr(fs, k, v)
        return fs

    path = 'modifiers["Fluid"].flow_settings.use_inflow'

    def toggle(obj, fs, schedule):
        for frame, on in schedule:
            fs.use_inflow = on
            obj.keyframe_insert(data_path=path, frame=frame)

    # 1) the missile's exhaust: a small hot emitter that flies in and leaves a trail
    bpy.ops.mesh.primitive_uv_sphere_add(radius=0.75, segments=24, ring_count=12, location=nozzle_at(1))
    mis = bpy.context.object
    mis.name = "MissileExhaust"
    fs = flow_object(mis, "BOTH", surface_distance=0.5, temperature=2.0, fuel_amount=0.3,
                     density=0.7, subframes=16, use_initial_velocity=False)
    mis.location = nozzle_at(1)
    mis.keyframe_insert("location", frame=1)
    mis.location = nozzle_at(T_IMPACT)
    mis.keyframe_insert("location", frame=T_IMPACT)
    toggle(mis, fs, [(1, True), (T_IMPACT, False)])

    # 2) the explosion itself: a big, hot, fast sphere
    bpy.ops.mesh.primitive_uv_sphere_add(radius=11.0, segments=48, ring_count=24, location=IMPACT)
    src = bpy.context.object
    src.name = "Charge"
    fs = flow_object(src, "BOTH", surface_distance=1.6, use_initial_velocity=True, velocity_normal=19.0,
                     temperature=6.5, fuel_amount=4.4, density=1.0, subframes=3)
    set_if(fs, velocity_random=2.5)
    toggle(src, fs, [(1, False), (T_IMPACT, True), (T_IMPACT + 3, False)])

    # 3) ground dust: a flat disc that kicks a ring of dust out along the floor (the shockwave)
    bpy.ops.mesh.primitive_cylinder_add(radius=14.0, depth=2.4, vertices=64, location=(0, 0, 1.4))
    ring = bpy.context.object
    ring.name = "DustRing"
    fs = flow_object(ring, "SMOKE", surface_distance=1.0, use_initial_velocity=True, velocity_normal=28.0,
                     temperature=0.4, density=2.2, subframes=3)
    toggle(ring, fs, [(1, False), (T_IMPACT, True), (T_IMPACT + 5, False)])
    return dom


def run_sim(a):
    out = os.path.abspath(a.out)
    bpy.ops.wm.read_factory_settings(use_empty=True)
    sc = bpy.context.scene
    sc.render.fps = FPS
    sc.frame_start, sc.frame_end = 1, a.frames
    make_sim_scene(a)
    with open(os.path.join(out, "sim_info.json"), "w") as f:
        json.dump({"res": a.res, "noise": a.noise, "frames": a.frames}, f)
    sc.frame_set(1)
    t0 = time.time()
    for f in range(1, a.frames + 1):
        t = time.time()
        sc.frame_set(f)                      # <- this steps the fire/smoke simulation
        log(f"simulated frame {f}/{a.frames} ({time.time() - t:.1f}s)")
    log(f"simulation done in {time.time() - t0:.0f}s")


# ------------------------------------------------------------------ RENDER STAGE

def setup_render(a):
    sc = bpy.context.scene
    sc.render.engine = "CYCLES"
    sc.render.fps = FPS
    sc.frame_start, sc.frame_end = 1, a.frames
    sc.render.resolution_x, sc.render.resolution_y = a.width, a.height
    sc.render.resolution_percentage = 100
    sc.render.image_settings.file_format = "PNG"
    sc.render.image_settings.color_depth = "16"
    cy = sc.cycles
    cy.samples = a.samples
    cy.use_adaptive_sampling = True
    cy.adaptive_threshold = 0.03
    try:                                     # not every Blender build ships the denoiser
        cy.denoiser = "OPENIMAGEDENOISE"
        cy.use_denoising = True
    except TypeError:
        cy.use_denoising = False
        log("no denoiser in this Blender build: use more --samples, and --denoise in explosion_post.py")
    cy.max_bounces = 4
    cy.diffuse_bounces = 2
    cy.glossy_bounces = 2
    cy.transmission_bounces = 2
    cy.volume_bounces = 1
    cy.volume_step_rate = a.step
    cy.volume_max_steps = 512
    cy.sample_clamp_indirect = 8
    sc.render.use_motion_blur = True         # sparks and debris streak like real high-speed footage
    set_if(sc.render, motion_blur_shutter=0.6)
    vs = sc.view_settings
    for name in ("AgX", "Filmic", "Standard"):
        try:
            vs.view_transform = name
            break
        except TypeError:
            continue
    for look in ("AgX - High Contrast", "High Contrast"):
        try:
            vs.look = look
            break
        except TypeError:
            continue
    vs.exposure = -0.3
    if a.gpu:
        try:
            prefs = bpy.context.preferences.addons["cycles"].preferences
            for kind in ("OPTIX", "CUDA", "HIP", "METAL", "ONEAPI"):
                try:
                    prefs.compute_device_type = kind
                    prefs.get_devices()
                    if any(d.type != "CPU" for d in prefs.devices):
                        for d in prefs.devices:
                            d.use = True
                        cy.device = "GPU"
                        log("GPU rendering:", kind)
                        break
                except TypeError:
                    continue
        except Exception as e:
            log("GPU setup failed, using CPU:", e)
    else:
        cy.device = "CPU"


def setup_world(haze):
    w = bpy.data.worlds.new("World")
    bpy.context.scene.world = w
    w.use_nodes = True
    nt = w.node_tree
    nt.nodes.clear()
    out = nt.nodes.new("ShaderNodeOutputWorld")
    bg = nt.nodes.new("ShaderNodeBackground")
    sky = nt.nodes.new("ShaderNodeTexSky")
    for t in ("MULTIPLE_SCATTERING", "NISHITA", "SINGLE_SCATTERING"):
        try:
            sky.sky_type = t
            break
        except TypeError:
            continue
    # a low sun in front of the camera: glowing horizon, backlit skyline
    set_if(sky, sun_elevation=math.radians(2.0), sun_rotation=SUN_ROTATION, air_density=2.2,
           dust_density=6.0, aerosol_density=6.0, ozone_density=1.0, sun_size=math.radians(1.5),
           sun_intensity=0.7)
    bg.inputs["Strength"].default_value = 0.38
    nt.links.new(sky.outputs[0], bg.inputs["Color"])
    nt.links.new(bg.outputs[0], out.inputs["Surface"])
    if haze:
        scat = nt.nodes.new("ShaderNodeVolumeScatter")
        scat.inputs["Density"].default_value = 0.0016
        scat.inputs["Color"].default_value = (0.9, 0.7, 0.55, 1)
        scat.inputs["Anisotropy"].default_value = 0.5
        nt.links.new(scat.outputs[0], out.inputs["Volume"])


def make_ground():
    bpy.ops.mesh.primitive_plane_add(size=1400, location=(0, 0, 0))
    g = bpy.context.object
    g.name = "Ground"
    m, nt = mat_node_tree("Ground")
    out = nt.nodes.new("ShaderNodeOutputMaterial")
    bsdf = nt.nodes.new("ShaderNodeBsdfPrincipled")
    tc = nt.nodes.new("ShaderNodeTexCoord")
    noise = nt.nodes.new("ShaderNodeTexNoise")
    noise.inputs["Scale"].default_value = 1.3
    noise.inputs["Detail"].default_value = 12
    noise.inputs["Roughness"].default_value = 0.7
    big = nt.nodes.new("ShaderNodeTexNoise")
    big.inputs["Scale"].default_value = 0.03
    big.inputs["Detail"].default_value = 6
    ramp = nt.nodes.new("ShaderNodeValToRGB")
    ramp.color_ramp.elements[0].color = (0.008, 0.007, 0.006, 1)
    ramp.color_ramp.elements[1].color = (0.040, 0.034, 0.028, 1)
    rough_ramp = nt.nodes.new("ShaderNodeValToRGB")
    rough_ramp.color_ramp.elements[0].color = (0.35, 0.35, 0.35, 1)       # wet patches
    rough_ramp.color_ramp.elements[1].color = (0.9, 0.9, 0.9, 1)
    bump = nt.nodes.new("ShaderNodeBump")
    bump.inputs["Strength"].default_value = 0.9
    nt.links.new(tc.outputs["Object"], noise.inputs["Vector"])
    nt.links.new(tc.outputs["Object"], big.inputs["Vector"])
    nt.links.new(big.outputs["Fac"], ramp.inputs["Fac"])
    nt.links.new(big.outputs["Fac"], rough_ramp.inputs["Fac"])
    nt.links.new(ramp.outputs["Color"], bsdf.inputs["Base Color"])
    nt.links.new(rough_ramp.outputs["Color"], bsdf.inputs["Roughness"])
    nt.links.new(noise.outputs["Fac"], bump.inputs["Height"])
    nt.links.new(bump.outputs[0], bsdf.inputs["Normal"])
    nt.links.new(bsdf.outputs[0], out.inputs["Surface"])
    g.data.materials.append(m)


def make_ruins(rnd):
    """Broken towers behind the blast, dark against the glowing sky; kept out of the camera's way."""
    mat = principled_mat("Ruin", (0.010, 0.009, 0.009), rough=0.95)
    back = math.atan2(VIEW.y, VIEW.x)              # direction pointing away from the camera
    for _ in range(46):
        ang = back + rnd.uniform(-1.2, 1.2)
        dist = rnd.uniform(110, 420)
        x, y = math.cos(ang) * dist, math.sin(ang) * dist
        w, d = rnd.uniform(14, 34), rnd.uniform(14, 34)
        height = rnd.uniform(28, 105) * (0.7 + dist / 420)
        yaw = rnd.uniform(0, math.pi)
        z, remaining = 0.0, height
        while remaining > 3:                        # stacked, shrinking, slightly crooked boxes = broken tower
            h = min(remaining, rnd.uniform(8, 30))
            bpy.ops.mesh.primitive_cube_add(size=1, location=(x + rnd.uniform(-2, 2), y + rnd.uniform(-2, 2), z + h / 2))
            o = bpy.context.object
            o.scale = (w, d, h)
            o.rotation_euler = Euler((rnd.uniform(-0.02, 0.02), rnd.uniform(-0.02, 0.02), yaw + rnd.uniform(-0.07, 0.07)))
            o.data.materials.append(mat)
            z += h
            remaining -= h
            w *= rnd.uniform(0.6, 0.92)
            d *= rnd.uniform(0.6, 0.92)
            if rnd.random() < 0.25:
                break
    for _ in range(70):                             # rubble around the blast, off the camera line
        ang = rnd.uniform(0, math.tau)
        if abs(math.remainder(ang - CAM_AZIMUTH, math.tau)) < 0.35:
            continue
        dist = rnd.uniform(16, 70)
        sz = rnd.uniform(0.6, 3.0)
        bpy.ops.mesh.primitive_cube_add(size=1, location=(math.cos(ang) * dist, math.sin(ang) * dist, sz * 0.3))
        o = bpy.context.object
        o.scale = (sz * rnd.uniform(0.6, 1.6), sz * rnd.uniform(0.6, 1.6), sz * rnd.uniform(0.3, 0.9))
        o.rotation_euler = Euler((rnd.uniform(-0.4, 0.4), rnd.uniform(-0.4, 0.4), rnd.uniform(0, math.pi)))
        o.data.materials.append(mat)


def read_sim_info(out):
    with open(os.path.join(out, "sim_info.json")) as f:
        return json.load(f)


def vdb_first_frame(out):
    base = os.path.join(out, "fluid")
    for sub, stem in (("noise", "fluid_noise"), ("data", "fluid_data")):
        f = os.path.join(base, sub, f"{stem}_0001.vdb")
        if os.path.isfile(f):
            return f
    sys.exit(f"No simulation found in {base}. Run --stage sim first (needs a Blender with working Mantaflow).")


def make_blast_volume(out, frames):
    """Load the simulated VDB frames as a plain volume and shade it as fire + smoke."""
    first = vdb_first_frame(out)
    vol = bpy.data.volumes.new("Blast")
    vol.filepath = first
    vol.is_sequence = True
    vol.frame_start = 1
    vol.frame_duration = frames
    vol.frame_offset = 0
    obj = bpy.data.objects.new("Blast", vol)
    bpy.context.scene.collection.objects.link(obj)
    bpy.context.scene.frame_set(1)           # sequence frame 0 does not exist
    vol.grids.load()
    # The cache stores the grid in its own units; map it onto the real simulation box.
    # Cell size (m) = longest box side / resolution; grid cell (0,0,0) is the box's low corner.
    info = read_sim_info(out)
    noisy = "fluid_noise" in first
    cell_m = max(DOMAIN) / info["res"] / (max(1, info.get("noise", 1)) if noisy else 1)
    dname, fname = ("density_noise", "flame_noise") if noisy else ("density", "flame")
    voxel = [g for g in vol.grids if g.name == dname][0].matrix_object[0][0]
    k = cell_m / voxel
    obj.scale = (k, k, k)
    obj.location = (-DOMAIN[0] / 2, -DOMAIN[1] / 2, 0.0)
    log("volume grids:", [g.name for g in vol.grids], f"cell {cell_m:.2f} m, scale {k:.4f}")

    m, nt = mat_node_tree("FireSmoke")
    o = nt.nodes.new("ShaderNodeOutputMaterial")
    pv = nt.nodes.new("ShaderNodeVolumePrincipled")
    pv.inputs["Density Attribute"].default_value = dname
    pv.inputs["Color Attribute"].default_value = ""
    pv.inputs["Temperature Attribute"].default_value = fname
    pv.inputs["Color"].default_value = (0.24, 0.205, 0.18, 1)
    pv.inputs["Density"].default_value = 4.0
    pv.inputs["Anisotropy"].default_value = 0.5
    pv.inputs["Emission Strength"].default_value = 0.0
    pv.inputs["Blackbody Intensity"].default_value = 22.0
    pv.inputs["Blackbody Tint"].default_value = (1, 0.85, 0.7, 1)
    pv.inputs["Temperature"].default_value = 3400.0
    nt.links.new(pv.outputs[0], o.inputs["Volume"])
    obj.data.materials.append(m)


def make_missile():
    """Visible missile that flies the same path the exhaust emitter flew in the sim."""
    interpolation("LINEAR")
    root = bpy.data.objects.new("Missile", None)
    bpy.context.scene.collection.objects.link(root)
    root.rotation_euler = MISSILE_DIR.to_track_quat("Z", "Y").to_euler()
    body_mat = principled_mat("MissileBody", (0.05, 0.05, 0.05), rough=0.4)
    glow_mat = principled_mat("MissileGlow", (1, 0.5, 0.1), emission=(1.0, 0.55, 0.15), strength=400.0)
    parts = []
    bpy.ops.mesh.primitive_cylinder_add(radius=0.38, depth=5.0, location=(0, 0, 0))
    body = bpy.context.object
    body.data.materials.append(body_mat)
    parts.append(body)
    bpy.ops.mesh.primitive_cone_add(radius1=0.38, radius2=0.0, depth=1.7, location=(0, 0, 3.35))
    nose = bpy.context.object
    nose.data.materials.append(body_mat)
    parts.append(nose)
    bpy.ops.mesh.primitive_uv_sphere_add(radius=0.55, location=(0, 0, -2.7))
    glow = bpy.context.object
    glow.data.materials.append(glow_mat)
    parts.append(glow)
    bpy.ops.object.light_add(type="POINT", location=(0, 0, -3.6))
    lamp = bpy.context.object
    lamp.data.color = (1.0, 0.5, 0.15)
    lamp.data.energy = 6.0e4
    lamp.data.shadow_soft_size = 1.0
    parts.append(lamp)
    for p in parts:
        p.parent = root
    root.location = MISSILE_START
    root.keyframe_insert("location", frame=1)
    root.location = IMPACT
    root.keyframe_insert("location", frame=T_IMPACT)
    for p in parts:                           # gone the moment it hits
        p.hide_render = False
        p.keyframe_insert("hide_render", frame=T_IMPACT - 1)
        p.hide_render = True
        p.keyframe_insert("hide_render", frame=T_IMPACT)


def make_sparks_and_debris():
    """Glowing sparks + dark flying chunks, thrown out at the moment of impact."""
    bpy.ops.mesh.primitive_uv_sphere_add(radius=6.0, segments=32, ring_count=16, location=IMPACT)
    src = bpy.context.object
    src.name = "Burst"
    spark_mat = principled_mat("Spark", (1, 0.4, 0.05), emission=(1.0, 0.45, 0.08), strength=80.0)
    bpy.ops.mesh.primitive_ico_sphere_add(radius=0.2, subdivisions=1, location=(0, 0, -50))
    spark = bpy.context.object
    spark.data.materials.append(spark_mat)
    chunk_mat = principled_mat("Chunk", (0.03, 0.026, 0.022), rough=0.9, emission=(1.0, 0.25, 0.03), strength=0.8)
    bpy.ops.mesh.primitive_cube_add(size=1, location=(0, 0, -60))
    chunk = bpy.context.object
    chunk.scale = (1.0, 0.7, 0.45)
    chunk.data.materials.append(chunk_mat)
    src.show_instancer_for_render = False

    def add_system(name, inst, count, vel, size, rand_size, life):
        bpy.context.view_layer.objects.active = src
        src.select_set(True)
        bpy.ops.object.particle_system_add()
        s = src.particle_systems[-1].settings
        src.particle_systems[-1].name = name
        s.type = "EMITTER"
        s.count = count
        s.frame_start, s.frame_end = T_IMPACT + 1, T_IMPACT + 4
        s.lifetime = life
        s.lifetime_random = 0.5
        s.emit_from = "FACE"
        s.normal_factor = vel
        s.factor_random = vel * 0.55
        s.render_type = "OBJECT"
        s.instance_object = inst
        s.particle_size = size
        s.size_random = rand_size
        s.use_rotations = True
        s.rotation_mode = "NOR"
        s.angular_velocity_mode = "RAND"
        s.angular_velocity_factor = 6.0
        s.physics_type = "NEWTON"
        s.mass = 1.0
        s.effector_weights.gravity = 1.0
        s.show_unborn = False
        s.use_dead = False
        set_if(s, drag_factor=0.04, damping=0.02)

    add_system("Sparks", spark, 1800, 70.0, 1.0, 0.9, 70)
    add_system("Debris", chunk, 150, 36.0, 1.3, 1.1, 90)
    bpy.context.scene.gravity = (0, 0, -9.81)


def make_camera(frames):
    interpolation("BEZIER")
    cam_data = bpy.data.cameras.new("Cam")
    cam = bpy.data.objects.new("Cam", cam_data)
    bpy.context.collection.objects.link(cam)
    bpy.context.scene.camera = cam
    cam_data.sensor_width = 36
    cam_data.dof.use_dof = True
    cam_data.dof.aperture_fstop = 11.0
    tgt = bpy.data.objects.new("Target", None)
    bpy.context.collection.objects.link(tgt)
    cons = cam.constraints.new("TRACK_TO")
    cons.target = tgt
    cons.track_axis = "TRACK_NEGATIVE_Z"
    cons.up_axis = "UP_Y"
    cam_data.dof.focus_object = tgt

    def pos(r, h):
        return (math.cos(CAM_AZIMUTH) * r, math.sin(CAM_AZIMUTH) * r, h)

    # wide while the missile comes in, then a slow push-in as the blast grows
    for frame, r, h, tz, lens in ((1, 236.0, 4.0, 24.0, 36.0), (T_IMPACT, 228.0, 4.5, 21.0, 38.0),
                                  (frames, 205.0, 8.0, 58.0, 43.0)):
        cam.location = pos(r, h)
        cam.keyframe_insert("location", frame=frame)
        tgt.location = (0, 0, tz)
        tgt.keyframe_insert("location", frame=frame)
        cam_data.lens = lens
        cam_data.keyframe_insert("lens", frame=frame)


def lighting():
    bpy.ops.object.light_add(type="SUN", rotation=(math.radians(75), 0, math.radians(30)))
    s = bpy.context.object
    s.data.energy = 0.12                        # cool fill so shadows are not pure black
    s.data.color = (0.55, 0.65, 0.9)
    # extra punch of orange light on the ground at the instant of impact
    bpy.ops.object.light_add(type="POINT", location=(0, 0, 9))
    p = bpy.context.object
    p.data.color = (1.0, 0.55, 0.2)
    p.data.shadow_soft_size = 7.0
    for frame, energy in ((T_IMPACT - 1, 0.0), (T_IMPACT + 1, 4.0e6), (T_IMPACT + 12, 1.2e6),
                          (T_IMPACT + 36, 2.0e5), (T_IMPACT + 70, 0.0)):
        p.data.energy = energy
        p.data.keyframe_insert("energy", frame=frame)


def build_render_scene(a, out):
    rnd = random.Random(a.seed)
    bpy.ops.wm.read_factory_settings(use_empty=True)
    setup_render(a)
    setup_world(a.haze)
    make_ground()
    make_ruins(rnd)
    make_blast_volume(out, a.frames)
    make_missile()
    make_sparks_and_debris()
    make_camera(a.frames)
    lighting()


def parse_only(spec, last):
    if not spec:
        return None
    out = []
    for part in spec.split(","):
        if "-" in part:
            x, y = part.split("-")
            out += list(range(int(x), int(y) + 1))
        elif part.strip():
            out.append(int(part))
    return [f for f in out if 1 <= f <= last]


def run_render(a):
    out = os.path.abspath(a.out)
    build_render_scene(a, out)
    sc = bpy.context.scene
    bpy.ops.wm.save_as_mainfile(filepath=os.path.join(out, "explosion_render.blend"))
    want = set(parse_only(a.only, a.frames) or range(1, a.frames + 1))
    t0 = time.time()
    # particles (sparks/debris) must be stepped from frame 1, so walk every frame, render the wanted ones
    for f in range(1, a.frames + 1):
        sc.frame_set(f)
        path = os.path.join(out, f"frame_{f:04d}.png")
        if f not in want or os.path.isfile(path):
            continue
        sc.render.filepath = path
        t = time.time()
        bpy.ops.render.render(write_still=True)
        log(f"frame {f}/{a.frames} rendered in {time.time() - t:.0f}s")
    log(f"render done in {time.time() - t0:.0f}s")


def main():
    a = get_args()
    os.makedirs(os.path.abspath(a.out), exist_ok=True)
    if a.stage in ("all", "sim"):
        run_sim(a)
    if a.stage in ("all", "render"):
        run_render(a)


if __name__ == "__main__":
    main()
