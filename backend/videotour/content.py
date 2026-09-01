"""Model-facing prompts for the video tour.

Written in English for every tenant, unlike acquisizione/content.py and
acquisizione/locales.py. Those produce text a seller reads, so they have to be
Italian or Slovak; nothing here ever reaches a human. The tenant's locale
changes the wizard copy, not what Seedance is told to render.

Two things shape both prompts:

  * No frame is pinned. Reference mode is what lets one generation cover
    several rooms, and it rules out keyframes (see videotour/runway.py). The
    prompts therefore have to describe the shot rather than rely on a start or
    end frame being fixed for them.
  * The references are addressed as @Image1…@ImageN by array position. That
    syntax is documented for Seedance multi-reference but is not visible as a
    field on promptImage in the API reference, so every prompt ALSO describes
    the images in plain order. If the markers are ignored, the prompt still
    reads correctly and the generation is still sensible — it just loses the
    explicit per-image binding.

The fidelity instruction is repeated deliberately. These are photographs of a
real property that a real buyer will act on: a model that invents a room, moves
a wall, or adds furniture has produced a misrepresentation, not a stylistic
liberty. It matters more than the cinematography.
"""

# ── gpt-image-2: 3D tile capture → photorealistic aerial ────────────────────
# The tile render is geometry, not a photograph: flat lighting, soft textures,
# and in smaller Italian and Slovak towns it can be genuinely low-detail. This
# pass is what turns it into something Seedance can use as a establishing
# reference for the zone.
AERIAL_PROMPT = (
    "This is a 3D map render of a real neighbourhood, seen from the air at an "
    "oblique angle. Convert it into a photorealistic aerial photograph of the "
    "same place, as if shot from a camera drone on a clear day with soft "
    "natural daylight.\n"
    "\n"
    "Keep the layout exactly as it is: the position, footprint, height and "
    "orientation of every building, the road layout, and the shape of open "
    "spaces must not change. Do not add or remove buildings, do not move "
    "streets, and do not redesign the area.\n"
    "\n"
    "What should change is only the rendering quality: replace flat, "
    "untextured or blurry surfaces with realistic roof, wall, road and "
    "vegetation materials; add natural directional sunlight with soft "
    "shadows consistent across the whole frame; and make foliage, parked "
    "cars and street furniture look photographic rather than modelled. The "
    "result should look like a real drone photograph of this exact place."
)


def drone_prompt() -> str:
    """The fly-in. Two references, in array order:

        @Image1 — the photoreal aerial, establishing the zone
        @Image2 — the agent's real photo of the actual building

    The aerial is a reference for WHERE the drone flies, not a frame to start
    on. The real photo is the subject: the shot has to arrive at that building,
    and it is the only image here that shows what the property actually looks
    like.
    """
    return (
        "A smooth, continuous cinematic drone shot of a real residential "
        "property.\n"
        "\n"
        "The first reference image (@Image1) is an aerial photograph of the "
        "neighbourhood where this property stands. Use it for the surrounding "
        "area only: the streets, the rooftops, the vegetation and the general "
        "look of the zone the drone flies over.\n"
        "\n"
        "The second reference image (@Image2) is a real photograph of the "
        "property itself. This building is the subject of the shot. Its "
        "shape, colour, materials, windows, doors, roof and surroundings must "
        "match that photograph exactly.\n"
        "\n"
        "The movement: begin high above the neighbourhood, looking down at the "
        "area from the reference aerial. Descend smoothly and continuously "
        "while flying forward toward the property, the camera gradually "
        "tilting from a downward view to a level one, until the building from "
        "the second reference fills the frame, seen from the front at roughly "
        "eye level. One single unbroken camera move, slow and steady, as if "
        "flown by a professional drone operator. No cuts, no jumps, no "
        "stutter, no speed ramps.\n"
        "\n"
        "Clear day, soft natural daylight, consistent shadows. "
        "Photorealistic throughout.\n"
        "\n"
        "Do not invent a different building. Do not change the property's "
        "architecture, proportions, number of floors, or the position of its "
        "windows and doors. Do not add buildings, vehicles, people or signage "
        "that are not in the reference images. No text or watermarks anywhere "
        "in the frame."
    )


def interior_prompt(room_count: int, seconds_per_room: int) -> str:
    """The single multi-room interior tour.

    References in array order:

        @Image1        — the exterior photo, so the clip opens on the building
                         the drone shot just arrived at
        @Image2…@ImageN — one interior photograph per room, in upload order

    One generation covers every room rather than one clip per room: it gives a
    single continuous camera language across the whole tour, removes N-1
    junctions from the stitch, and caps cost at Seedance's 30-second ceiling
    however many rooms the agent uploads.
    """
    room_markers = ", ".join("@Image%d" % (i + 2) for i in range(room_count))
    return (
        "A continuous cinematic walkthrough of the interior of a real "
        "residential property, moving from room to room.\n"
        "\n"
        "The first reference image (@Image1) is the exterior of the building. "
        "Open on that exterior, at the entrance, then move inside.\n"
        "\n"
        "The remaining %d reference images (%s) are photographs of the "
        "interior. IMPORTANT: each of these images is a DIFFERENT, SEPARATE "
        "room of the property. They are not variations of one room and they "
        "are not different angles of the same space. Give each image its own "
        "scene of roughly %d seconds, in the order listed, so the video moves "
        "through the rooms one at a time.\n"
        "\n"
        "Within each room, the camera moves slowly and steadily — a gentle "
        "forward push, or a slow pan across the space — as if walking through "
        "with a stabilised camera. Between rooms, move through the space "
        "naturally, as though walking from one room into the next. Keep the "
        "motion unhurried and even throughout. No cuts, no jumps, no fast "
        "movements, no speed ramps.\n"
        "\n"
        "Each room must match its reference photograph exactly. Do NOT invent "
        "the geometry of any room: keep the walls, floor, ceiling, room "
        "proportions and layout exactly as photographed, and keep every "
        "window, door and built-in fixture in the same place, at the same "
        "size, in the same shape. Do not add, remove, move or restyle any "
        "furniture. Do not open up walls, extend rooms, or invent space that "
        "is not visible in the photograph. Reveal only what the photograph "
        "actually shows.\n"
        "\n"
        "Natural interior daylight, consistent with each photograph. "
        "Photorealistic throughout. No people, no text, no watermarks."
        % (room_count, room_markers, seconds_per_room)
    )
