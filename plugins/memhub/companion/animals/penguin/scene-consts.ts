// The scene's fixed geometry, split out so the choreography can read it
// without importing the drawing (which imports the frames it yields).

export const W = 22
export const H = 14
/** The row the feet stand on. */
export const FOOT = 12
/**
 * One row of ice, right under the feet: like the goose's grass it shares a
 * cell with them, so the gaps between the legs show ice and nobody stands in
 * it.
 */
export const ICE_Y = 13
/** The penguin sprite's left edge at rest. */
export const PX = 5
/** The columns the whale comes up through, inclusive. */
export const HOLE: readonly [number, number] = [14, 21]
/** How long a splash lasts, in frames. */
export const SPLASH_LEN = 12
