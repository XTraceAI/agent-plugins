// The scene's fixed geometry, split out so the art and the choreography can
// read it without importing the drawing (which imports the frames it yields).

export const W = 22
export const H = 12
/** The grass row; everything stands on row 10 above it. */
export const GROUND = 11
/** The bottom row of every standing sprite. */
export const FLOOR = 10
/** The dog's front edge when at home. */
export const HOME = 6
