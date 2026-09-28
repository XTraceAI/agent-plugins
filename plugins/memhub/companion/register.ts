import type { EngineInterface, On, Timer } from 'claude-code'

import type { Animal, Build, Painted, Tone } from './animal'
import { ANIMALS, DEFAULT_ANIMAL } from './animals'
import { type Fired, firedOf } from './rules'
import { compose, encode, fitBubble, type Layout, layoutOf, SCALES } from './screen'

/**
 * The companion in the band above the prompt: MemHub's face in the session,
 * there to show what the plugin is doing for it. It sleeps while nothing
 * happens, looks around while Claude answers or you type, and rises to speak
 * when a MemHub rule fires, announcing the rule — calmly for an advisory,
 * crossly for a call the rule stopped.
 *
 * This is the whole of what watches the session. Which animal does the
 * sleeping and speaking is the Animal contract's business (see animal.ts):
 * the director asks for a pose and draws what comes back, and knows nothing
 * else about it. Every pose plays whole; it cuts in only where the animal is
 * merely waiting (asleep, looking).
 *
 * `/hippo`, `/goose`, ... each switch to that animal and turn the band on;
 * every one also takes `off`, `on`, `auto`, and — where the animal offers a
 * demo — `demo` and its own pose names.
 */

const FPS = 20
const RASTER_KEY = 'companion'
const STORE_ENABLED = 'companion.enabled'
const STORE_ANIMAL = 'companion.animal'
const STORE_SCALE = 'companion.scale'
/** How long a keystroke keeps the animal looking, and a finished turn too. */
const LINGER_FRAMES = 3 * FPS
/** Fires waiting to be announced; past this the oldest waiting one drops. */
const MAX_QUEUED = 3

/** A pose the director asks the animal for, plus its own idle `offscreen`. */
type PoseName = 'offscreen' | 'enter' | 'sleep' | 'wake' | 'look' | 'rise' | 'speak' | 'leave' | 'demo'

type Segment = { name: PoseName; frames: Iterator<unknown> }

/** What the session wants of the animal right now. */
type Want = 'sleep' | 'look' | 'speak'

/** Everything the companion keeps between frames and events. */
type Band = {
  animal: Animal
  /** The animal's art for the size being drawn; its poses are the ones running. */
  build: Build
  fires: number
  queue: Fired[]
  isEnabled: boolean
  /** A forced pixel size, or null to let the room pick as hippo_half.py does. */
  scale: number | null
  /** Rows the band really gave the drawing, once it has said; see ui.render. */
  roomRows: number | null
  /** The width and allowance the room was measured at; a change measures again. */
  roomAt: number | null
  roomMax: number | null
  /** `auto` follows the session; anything else is the animal's own demo. */
  mode: string
  isWorking: boolean
  /** The frame until which a keystroke or a finished turn keeps it looking. */
  activeUntil: number
  seg: Segment
  frame: number
  current: Painted
  timer: Timer | null
  requestId: string | null
  layout: Layout | null
  lastCells: string
}

export function register(on: On) {
  const band: Band = {
    animal: DEFAULT_ANIMAL,
    build: DEFAULT_ANIMAL.builds[0]!,
    fires: 0,
    queue: [],
    isEnabled: true,
    scale: null,
    roomRows: null,
    roomAt: null,
    roomMax: null,
    mode: 'auto',
    isWorking: false,
    activeUntil: 0,
    seg: { name: 'offscreen', frames: [][Symbol.iterator]() },
    frame: 0,
    current: { canvas: [] },
    timer: null,
    requestId: null,
    layout: null,
    lastCells: '',
  }

  on('session.start', async ($, e, next) => {
    const result = await next(e)
    if (e.surface !== 'terminal' || !e.isInteractive) {
      return result
    }
    band.isEnabled = (await $.store.get(STORE_ENABLED).catch(() => undefined)) !== false
    const savedScale = await $.store.get(STORE_SCALE).catch(() => undefined)
    band.scale = SCALES.includes(savedScale as never) ? (savedScale as number) : null
    const saved = await $.store.get(STORE_ANIMAL).catch(() => undefined)
    band.animal = ANIMALS.find(a => a.name === saved) ?? DEFAULT_ANIMAL
    for (const animal of ANIMALS) {
      await $.command
        .register({
          name: commandOf(animal),
          description: `The MemHub ${animal.name.toLowerCase()} above the prompt: what the plugin is doing, as it happens`,
          argumentHint: usageOf(animal),
          immediate: true,
        })
        .catch(() => undefined)
    }
    if (band.isEnabled) {
      start($, band)
    }
    return result
  })

  for (const animal of ANIMALS) {
    on('command.run', { command: commandOf(animal) }, async ($, e) => {
      const arg = e.args.trim().toLowerCase()
      if (arg === 'off') {
        band.isEnabled = false
        stop(band)
        await $.store.set(STORE_ENABLED, false).catch(() => undefined)
        $.ui.invalidate('ui.render')
        return { text: `The ${animal.name.toLowerCase()} goes away. /${commandOf(animal)} brings it back.` }
      }
      const scaleAsked = /^scale +([1-4])$/.exec(arg)
      if (scaleAsked || arg === 'scale auto') {
        band.scale = scaleAsked ? Number(scaleAsked[1]) : null
        await $.store.set(STORE_SCALE, band.scale).catch(() => undefined)
        $.ui.invalidate('ui.render')
        const fits = band.layout ? ` (showing ${band.layout.s})` : ''
        return { text: `${commandOf(animal)}: pixel size ${band.scale ?? 'auto'}${fits}` }
      }
      const isDemo = arg !== '' && arg !== 'on' && arg !== 'auto'
      const demoBuild = buildFor(animal, band.scale)
      if (isDemo && !(demoBuild.demo && (arg === 'demo' || (demoBuild.demoPoses ?? []).includes(arg)))) {
        return { text: `usage: /${commandOf(animal)} ${usageOf(animal)}` }
      }
      const isSwitch = band.animal !== animal || !band.isEnabled
      band.isEnabled = true
      band.animal = animal
      await $.store.set(STORE_ENABLED, true).catch(() => undefined)
      await $.store.set(STORE_ANIMAL, animal.name).catch(() => undefined)
      if (isDemo) {
        play(band, arg)
      } else if (isSwitch) {
        // a new animal starts offscreen: it is not standing where the last one stood
        band.mode = 'auto'
        band.build = buildFor(animal, band.scale)
        band.seg = segment(band, 'offscreen')
        band.current = { canvas: [] }
      } else if (band.mode !== 'auto') {
        play(band, 'auto')
      }
      start($, band)
      $.ui.invalidate('ui.render')
      return {
        text:
          band.mode === 'auto'
            ? `${animal.name.toLowerCase()}: following the session`
            : `${animal.name.toLowerCase()}: looping ${band.mode}`,
      }
    })
  }

  on('prompt.edit', ($, e, next) => {
    band.activeUntil = band.frame + LINGER_FRAMES
    nudge(band)
    return next(e)
  })

  on('turn.start', async ($, e, next) => {
    const result = await next(e)
    band.isWorking = true
    nudge(band)
    return result
  })

  on('turn.complete', async ($, e, next) => {
    const result = await next(e)
    if (e.agentId === undefined) {
      band.isWorking = false
      band.activeUntil = band.frame + LINGER_FRAMES
    }
    return result
  })

  // The rulebook's command hooks sit beneath every hooks module in the classic
  // chain, so `next(e)` is what they answered for this call.
  on('classic.PreToolUse', async ($, e, next) => {
    const result = await next(e)
    announce(band, firedOf(result.additionalContext ?? [], result.deny ?? result.ask))
    return result
  })

  on('classic.PostToolUse', async ($, e, next) => {
    const result = await next(e)
    announce(band, firedOf(result.additionalContext ?? [], result.block))
    return result
  })

  on('ui.render', { component: 'AbovePrompt' }, ($, e, next) => {
    if (!band.isEnabled || e.surface !== 'terminal' || e.props.hasSurvey) {
      band.requestId = null
      return next(e)
    }
    const { Box, Text, Raster } = $.ui.resolve(e)
    band.requestId = e.requestId
    // hippo_half.py picks its pixel size from the terminal's height; the band
    // gets less than that, and only says how much by how much it scrolled. So
    // pick from the allowance, and where the drawing did not fit, remember the
    // rows it actually got and pick again — shrinking until it sits whole.
    if (band.roomAt !== e.props.bodyColumns || band.roomMax !== e.props.maxRows) {
      band.roomAt = e.props.bodyColumns
      band.roomMax = e.props.maxRows
      band.roomRows = null
    }
    const room = Math.max(1, Math.min(e.props.maxRows, band.roomRows ?? e.props.maxRows))
    const build = buildFor(band.animal, band.scale)
    if (build !== band.build) {
      // different art, different frames: it starts over rather than cutting
      // from one drawing's pose into another's
      band.build = build
      band.seg = segment(band, 'offscreen')
      band.current = { canvas: [] }
    }
    band.layout = layoutOf(build, e.props.bodyColumns, room, band.scale ?? undefined)
    if (!band.layout) {
      const name = band.animal.name.toLowerCase()
      return Text({ dimColor: true, children: `make the terminal a little bigger for the ${name}` })
    }
    const { bodyRows } = e.props.scroll
    if (bodyRows > 0 && bodyRows < band.layout.rows && band.roomRows !== bodyRows) {
      band.roomRows = bodyRows
      $.ui.invalidate('ui.render')
    }
    band.lastCells = cellsOf(band) ?? ''
    return Box({
      justifyContent: 'flex-end',
      children: Raster({
        key: RASTER_KEY,
        columns: band.layout.columns,
        rows: band.layout.rows,
        cells: band.lastCells,
      }),
    })
  })
}

const commandOf = (animal: Animal) => animal.name.toLowerCase()

/** What its bubble introduces it as. */
const titleOf = (animal: Animal) => animal.title ?? animal.name

/**
 * The build to draw: the largest whose art is drawn for a pixel no bigger than
 * the one asked for, and the smallest when nothing is asked — small is the
 * default, and a bigger pixel is `/<animal> scale <n>`'s to ask for.
 */
function buildFor(animal: Animal, scale: number | null): Build {
  const builds = [...animal.builds].sort((a, b) => a.pixelSize - b.pixelSize)
  if (scale === null) {
    return builds[0]!
  }
  const fits = builds.filter(b => b.pixelSize <= scale)
  return (fits[fits.length - 1] ?? builds[0])!
}

const usageOf = (animal: Animal) =>
  ['on', 'off', 'auto', 'scale 1-4',
   ...(animal.builds[0]?.demo ? ['demo', ...(animal.builds[0]?.demoPoses ?? [])] : [])].join('|')

function wantOf(band: Band): Want {
  if (band.queue.length > 0) return 'speak'
  return band.isWorking || band.frame < band.activeUntil ? 'look' : 'sleep'
}

function announce(band: Band, fired: readonly Fired[]) {
  if (fired.length === 0) return
  band.queue.push(...fired)
  band.queue.splice(0, Math.max(0, band.queue.length - MAX_QUEUED))
  nudge(band)
}

function segment(band: Band, name: PoseName): Segment {
  const { poses } = band.build
  switch (name) {
    case 'offscreen': return { name, frames: [][Symbol.iterator]() }
    case 'enter': return { name, frames: poses.enter() }
    case 'sleep': return { name, frames: poses.sleep() }
    case 'wake': return { name, frames: poses.wake() }
    case 'look': return { name, frames: poses.look() }
    case 'rise': return { name, frames: poses.rise() }
    case 'leave': return { name, frames: poses.leave() }
    case 'demo': return { name, frames: band.build.demo!(band.mode === 'demo' ? null : band.mode) }
    case 'speak': {
      const fire = band.queue.shift()!
      band.fires += 1
      const tone: Tone = fire.isBlocked ? 'blocked' : 'advice'
      const said = fitBubble(fire.isBlocked ? `Blocked: ${fire.rule}` : `Rule fired: ${fire.rule}`)
      return { name, frames: poses.speak(said, band.fires, tone) }
    }
  }
}

/** What follows a pose that played to its end. */
function after(band: Band, name: PoseName): PoseName {
  const want = wantOf(band)
  switch (name) {
    case 'offscreen': return 'enter'
    // enter and sleep both leave the animal asleep; wake is what opens its eyes
    case 'enter': return want === 'sleep' ? 'sleep' : 'wake'
    case 'wake': return want === 'speak' ? 'rise' : want === 'look' ? 'look' : 'sleep'
    case 'rise': return want === 'speak' ? 'speak' : 'leave'
    case 'speak': return want === 'speak' ? 'speak' : 'leave'
    case 'leave': return 'enter'
    default: return name // sleep, look, demo: endless
  }
}

/** Cut in where the animal is only waiting; everything else plays out. */
function nudge(band: Band) {
  if (band.mode !== 'auto') {
    return
  }
  const want = wantOf(band)
  const name = band.seg.name
  if (name === 'sleep' && want !== 'sleep') {
    band.seg = segment(band, 'wake')
  } else if (name === 'look' && want === 'speak') {
    band.seg = segment(band, 'rise')
  } else if (name === 'look' && want === 'sleep') {
    band.seg = segment(band, 'sleep')
  }
}

function play(band: Band, mode: string) {
  band.mode = mode
  band.seg = segment(band, mode === 'auto' ? 'leave' : 'demo')
}

function advance(band: Band): unknown {
  for (;;) {
    const r = band.seg.frames.next()
    if (!r.done) {
      return r.value
    }
    band.seg = segment(band, after(band, band.seg.name))
  }
}

function cellsOf(band: Band): string | null {
  return band.layout
    ? encode(compose(band.current, band.build, titleOf(band.animal), band.layout))
    : null
}

function start($: EngineInterface, band: Band) {
  band.timer ??= $.clock.every(1000 / FPS, () => void tick($, band))
}

function stop(band: Band) {
  band.timer?.cancel()
  band.timer = null
  band.requestId = null
}

/** One frame: hippo_half.py's main loop body, blitting where it used to flush. */
async function tick($: EngineInterface, band: Band) {
  // the linger running out is a change of want with no event behind it
  if (band.frame === band.activeUntil) {
    nudge(band)
  }
  band.current = band.build.render(advance(band), band.frame)
  const cells = cellsOf(band)
  band.frame += 1
  const { requestId } = band
  if (requestId && cells && cells !== band.lastCells) {
    band.lastCells = cells
    await $.ui.blit({ requestId, key: RASTER_KEY, cells }).catch(() => undefined)
  }
}
