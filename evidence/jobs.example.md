# Jobs — software rasterizer, four stages

Supplied by the caller. One job per translation unit; four disjoint write
scopes, no `depends_on`, so all four are eligible for one wave. What replaces
sequencing is the frozen contracts in each block — both sides of every boundary
build against them without ever seeing each other's worktree.

Tiers: `st-3-clip` is `t3` because the interpolation rule at a generated vertex
is the one subtlety in the assignment that fails silently. The rest are the
workhorse and the trivial one.

```yaml
- id: st-1-buffers
  goal: >
    Implement initialize_render in driver_state.cpp: set state.image_width and
    state.image_height, delete[] any previous buffers, allocate image_color and
    image_depth with new[] of width*height, fill image_color with
    make_pixel(0,0,0) and image_depth with std::numeric_limits<float>::max()
    (include <limits>). Delete the TODO cout line. The existing destructor
    already delete[]s both. Roughly 15 lines. `scons -Q` must stay green.
    Expect `sh check-grade.sh` to still score 0/31 with only this stage done —
    that is correct and not a failure of your job.
  file_scope: ["driver_state.cpp"]
  reads: ["driver_state.h", "common.h", "vec.h", "main.cpp", "parse.cpp", "SConstruct"]
  frozen_interfaces:
    - "void initialize_render(driver_state& state, int width, int height);"
    - "FI-4: both buffers are indexed [j*image_width + i], i in [0,width), j in [0,height), and j==0 is the BOTTOM row."
    - "FI-4: image_color initialised to make_pixel(0,0,0); image_depth to std::numeric_limits<float>::max()."
  tier: t1
  acceptance:
    - "initialize_render allocates both buffers and main.cpp's compare() no longer crashes"

- id: st-2-render
  goal: >
    Implement render in render.cpp: per-vertex shader invocation and triangle
    assembly for all four render types, handing clip-space triangles to
    clip_triangle. Triangle counts — `triangle`: num_vertices/3 with vertices
    (3t, 3t+1, 3t+2); `fan`: num_vertices-2 with (0, i, i+1); `strip`:
    num_vertices-2 with (i, i+1, i+2); `indexed`: state.num_triangles with
    index_data[3t..3t+2]. state.num_triangles is 0 for the first three types
    because parse.cpp only sets it from the `f` lines, so deriving the count
    from num_vertices is mandatory, not defensive. Per vertex, build
    data_vertex in{&state.vertex_data[idx*state.floats_per_vertex]}, copy
    floats_per_vertex floats into the output buffer FIRST (the shipped vertex
    shaders write only the colour slots and leave the rest uninitialised), then
    call state.vertex_shader(in, out, state.uniform_data). No heap allocation,
    no caching. Delete the TODO cout line. Roughly 95 lines. Expect
    `sh check-grade.sh` to still score 0/31 with only this stage done.
  file_scope: ["render.cpp"]
  reads: ["driver_state.h", "common.h", "vec.h", "mat.h", "shaders.h", "shaders.cpp", "parse.cpp", "SConstruct"]
  frozen_interfaces:
    - "void render(driver_state& state, render_type type);"
    - "FI-2: leave gl_Position exactly as the vertex shader wrote it — clip space, homogeneous. Do NOT divide by w. The divide happens once, in rasterize_triangle."
    - "FI-6: call clip_triangle(state, g0, g1, g2, 0) for every assembled triangle. Never call rasterize_triangle directly."
    - "FI-3: each data_geometry::data points at a stack float[MAX_FLOATS_PER_VERTEX] owned by this frame, pre-filled with the vertex's floats_per_vertex input values before the shader runs."
  tier: t2
  acceptance:
    - "all four render types assemble the right triangles and reach clip_triangle in clip space"

- id: st-3-clip
  goal: >
    Implement clip_triangle in clip.cpp: recursive clipping against the six
    clip-space faces, with interpolation of attributes at generated vertices
    that follows state.interp_rules. Test each face in homogeneous form, never
    against NDC ±1 — the NDC form breaks when w is negative and loses precision
    near the near plane. Faces: 0: w+x>=0, 1: w-x>=0, 2: w+y>=0, 3: w-y>=0,
    4: w+z>=0, 5: w-z>=0. For a plane f(V)>=0 the crossing parameter on edge
    A->B is t = f(A)/(f(A)-f(B)). All three inside: recurse to face+1 unchanged.
    One outside: two new vertices, two sub-triangles. Two outside: one new
    vertex each, one sub-triangle. Preserve winding in every emitted triangle
    and recurse each with face+1. Depth is bounded, so plain recursion is right
    — do not add a queue. Delete the TODO cout line: it fires once per triangle
    and one scene has 23216 of them under a 4-second timeout. Roughly 120 lines.
    Expect `sh check-grade.sh` to still score 0/31 with only this stage done.
    READ FI-7 BEFORE WRITING THE INTERPOLATION LOOP — it is the one thing here
    that fails silently, and it decides a scene on its own.
  file_scope: ["clip.cpp"]
  reads: ["driver_state.h", "common.h", "vec.h", "parse.cpp", "SConstruct"]
  frozen_interfaces:
    - "void clip_triangle(driver_state& state, const data_geometry& v0, const data_geometry& v1, const data_geometry& v2, int face);  // the definition must NOT repeat the =0 default argument"
    - "FI-2: gl_Position in and out is clip space, homogeneous. Never divide by w in this file."
    - "FI-3: generated vertices own stack float[MAX_FLOATS_PER_VERTEX] buffers in the recursing frame. Never free or write through an incoming data pointer."
    - "FI-7: at a generated vertex P = A + t*(B-A), each attribute float k follows state.interp_rules[k]: smooth -> lerp by t; noperspective -> lerp by the SCREEN-space parameter s = t*w_B/w_P where w_P = (1-t)*w_A + t*w_B; flat -> copy v0.data[k] of the incoming triangle. Lerping a noperspective attribute by t is wrong whenever w varies along the clipped edge, and the error is smeared affinely across the whole visible sub-triangle."
  hotspots: ["clip.cpp"]
  tier: t3
  acceptance:
    - "all six faces clip geometrically and generated vertices interpolate by interp_rules, not by t alone"

- id: st-4-raster
  goal: >
    Implement rasterize_triangle in raster.cpp: viewport transform, barycentric
    inside test, z-buffering, the three interpolation modes, and the fragment
    shader call. Barycentrics from twice-signed-area so the inside test is
    winding-independent (nothing in this driver culls):
    area = (x1-x0)*(y2-y0) - (x2-x0)*(y1-y0), bail if zero;
    a0 = ((x1-px)*(y2-py) - (x2-px)*(y1-py))/area,
    a1 = ((x2-px)*(y0-py) - (x0-px)*(y2-py))/area, a2 = 1-a0-a1; inside iff all
    >= 0. Bounding box from floor/ceil of the three screen x/y, CLAMPED to
    [0,width-1] and [0,height-1] before looping. Depth z = a0*z0+a1*z1+a2*z2 on
    NDC z. Interpolation per state.interp_rules[k]: flat -> v0.data[k];
    noperspective -> a0*d0+a1*d1+a2*d2; smooth -> perspective-correct, with
    denom = a0/w0+a1/w1+a2/w2 and weights (a_i/w_i)/denom. NOTE the stub has a
    fallthrough bug: its `case smooth:` has no break before `case
    noperspective:`. Fragment data must be ONE stack float[MAX_FLOATS_PER_VERTEX]
    reused across pixels, not the stub's per-pixel `new float[...]` — that leaks
    once per fragment and one scene is 500x500 with 23216 triangles under a
    4-second timeout. Colour out: make_pixel of clamp(c,0,1)*255, truncating.
    Delete the TODO cout line. Roughly 110 lines. Expect `sh check-grade.sh` to
    still score 0/31 with only this stage done.
  file_scope: ["raster.cpp"]
  reads: ["driver_state.h", "common.h", "vec.h", "shaders.h", "shaders.cpp", "main.cpp", "SConstruct"]
  frozen_interfaces:
    - "void rasterize_triangle(driver_state& state, const data_geometry& v0, const data_geometry& v1, const data_geometry& v2);"
    - "FI-2: gl_Position arrives in clip space. This function performs the one and only perspective divide."
    - "FI-4: index [j*state.image_width + i]; keep a fragment iff z < state.image_depth[idx], then write BOTH image_depth[idx] and image_color[idx]."
    - "FI-5: px = (image_width*0.5f)*x_ndc + (image_width*0.5f) - 0.5f; py = (image_height*0.5f)*y_ndc + (image_height*0.5f) - 0.5f."
  hotspots: ["raster.cpp"]
  tier: t2
  acceptance:
    - "all three interpolation modes present, z-buffer correct, no per-fragment allocation"
```
