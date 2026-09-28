// The scene's fixed geometry, split out so the choreography can read it
// without importing the drawing (which imports the frames it yields).

export const W = 22
export const H = 12
/** The standing goose's box: 13 x 7, rows 4-10. */
export const GX = 6
export const GY = 4
/** One row of grass; feet stand on row 10 above it. */
export const GROUND_Y = 11
/** A gripped squid's box, relative to the flyer's. */
export const GRIP_DY = 6
