// Every animal the band can show. A new one lands here and nowhere else: the
// director reads this list for its commands and for the saved choice.
//
// See docs/companion/ANIMALS.md for what writing one involves.

import type { Animal } from '../animal'
import { goose } from './goose'
import { hippo } from './hippo'
import { penguin } from './penguin'

// Gus first: the band shows the first of these to anyone who has not picked
// one, and a saved choice wins over it.
export const ANIMALS: readonly Animal[] = [goose as Animal, hippo as Animal, penguin as Animal]

export const DEFAULT_ANIMAL = ANIMALS[0]!
