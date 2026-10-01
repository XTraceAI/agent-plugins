import type { EngineInterface, On, Timer } from 'claude-code'

import type { Animal, Build, Painted, Tone } from './animal'
import { ANIMALS, DEFAULT_ANIMAL } from './animals'
import { type Decision, decisionSaid, nameOf, type Proposed, proposalSaid, proposedOf, RULE_WORD } from './proposed'
import { type Fired, firedOf } from './rules'
import { BUB_BG_HEX, compose, encode, fitBubble, footerOf, type Layout, layoutOf, petAt, SCALES, wordAt } from './screen'
import { pick, poolOf, prefsOf, type Prefs, remember, sessionsOf } from './selection'

/**
 * The companion in the band above the prompt: MemHub's face in the session,
 * there to show what the plugin is doing for it. It sleeps while nothing
 * happens, looks around while Claude answers or you type, and rises to speak
 * when a MemHub rule fires, announcing the rule — calmly for an advisory,
 * crossly for a call the rule stopped — and when the harness has proposed a
 * new rule, presenting it and asking: Activate, Reject or Later, as buttons
 * inside its bubble (1, 2, 3 from an empty prompt) until it is answered.
 *
 * This is the whole of what watches the session. Which animal does the
 * sleeping and speaking is the Animal contract's business (see animal.ts):
 * the director asks for a pose and draws what comes back, and knows nothing
 * else about it. Every pose plays whole; it cuts in only where the animal is
 * merely waiting (asleep, looking).
 *
 * Which animal shows is the session's own (see selection.ts): picked from the
 * session id unless someone pinned one, and changed for this session alone by
 * `/hippo`, `/goose`, ... — each of which also takes `always` (pin it for
 * every new session), `random` (drop the pin), `never` / `include` (leave it
 * out of, or put it back in, the random pick), `off`, `on`, `auto`, and —
 * where the animal offers a demo — `demo` and its own pose names.
 *
 * A click on the ♥ beside its ground pets it. The animal itself cannot take
 * the click: a Raster takes none, and a surface module laid over it to take
 * them froze the pixels under it, while one laid beneath it never got them. A
 * proposal's `rule↗` opens the rule in MemHub Studio.
 */

const FPS = 20
const RASTER_KEY = 'companion'
const STORE_ENABLED = 'companion.enabled'
// `companion.animal`, the old global pick, is neither read nor written: most
// of it was `/goose` run to turn the band on, not a choice. Older versions
// still read it, so it is left where it is.
const STORE_PIN = 'companion.pin'
const STORE_NEVER = 'companion.never'
const STORE_SESSIONS = 'companion.sessions'
const STORE_SCALE = 'companion.scale'
/** How long a keystroke keeps the animal looking, and a finished turn too. */
const LINGER_FRAMES = 3 * FPS
/** Announcements waiting their turn; past this the oldest waiting one drops. */
const MAX_QUEUED = 3
/** The key of the ♥ at the animal's feet that pets it. */
const PET_KEY = 'companion-pet'
/** How long a click waits for the animal to be free to be petted; then it drops. */
const PET_PENDING_FRAMES = 5 * FPS
/** The director's own pet, for an animal without one: hearts over `look`. */
const FALLBACK_PET_FRAMES = 30
const HEART_RGB: [number, number, number] = [240, 124, 150]
/** How long after a /clear or /resume the frame clock keeps asking for the new session id. */
const REPICK_FRAMES = 10 * FPS

/** A pose the director asks the animal for, plus its own idle `offscreen`. */
type PoseName = 'offscreen' | 'enter' | 'sleep' | 'wake' | 'look' | 'rise' | 'speak' | 'leave' | 'demo' | 'pet'

type Segment = { name: PoseName; frames: Iterator<unknown> }

/** One thing the animal has to say, in the bubble's tone. */
type Said = { text: string; tone: Tone }

/** What the session wants of the animal right now. */
type Want = 'sleep' | 'look' | 'speak'

/** Everything the companion keeps between frames and events. */
type Band = {
  animal: Animal
  /** The animal's art for the size being drawn; its poses are the ones running. */
  build: Build
  fires: number
  queue: Said[]
  /** Proposed rules waiting on the person's answer; the first has the buttons. */
  asks: Proposed[]
  /** Rule ids already announced this session: the server lists a proposal until it is decided. */
  announced: Set<string>
  /** An answer is on its way to MemHub; the buttons wait for it. */
  isDeciding: boolean
  /** The demo is showing a proposal, so it shows the buttons too. */
  hasDemoAsk: boolean
  /** What a press in the demo would have done, said in place of doing it. */
  demoNote: string | null
  /** The frame the demo's note gives the buttons back. */
  demoNoteUntil: number
  /** Where the buttons sat when last drawn (see buttonsAt), so a move redraws. */
  buttonsKey: string
  /** Where the proposal's linked `rule` sat when last drawn (see wordOf), likewise. */
  wordKey: string
  /** The tone of what the animal is saying now, while a `speak` plays. */
  speakingTone: Tone | null
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
  /** The session the animal was picked for. */
  sessionId: string
  /**
   * A /clear or /resume moved the process to another session: pick again once
   * its id is readable — tried on every prompt edit and turn until then, and
   * by the frame clock until `repickUntil`.
   */
  needsRepick: boolean
  repickUntil: number
  isPicking: boolean
  /** Bumped by every command that picks an animal, so a pick already in flight yields to it. */
  pickGen: number
  /** An animal picked by a command before the engine reported the new session's id: it is that session's. */
  pendingPick: string | null
  /** A click waits, until this frame, for the animal to be free to be petted. */
  petUntil: number
  /** The frame the director's own pet began, for its hearts. */
  petAt: number
  /** Got it: the speech's bubble is hidden and its holds skipped. */
  isHushed: boolean
  /** The last frame a hushed speech showed, so a hold's repeats are skipped. */
  hushedFrame: string
  /** A page is being opened in the browser; another press waits for it. */
  isOpening: boolean
}

export function register(on: On) {
  const band: Band = {
    animal: DEFAULT_ANIMAL,
    build: DEFAULT_ANIMAL.builds[0]!,
    fires: 0,
    queue: [],
    asks: [],
    announced: new Set(),
    isDeciding: false,
    hasDemoAsk: false,
    demoNote: null,
    demoNoteUntil: 0,
    buttonsKey: '',
    wordKey: '',
    speakingTone: null,
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
    sessionId: '',
    needsRepick: false,
    repickUntil: 0,
    isPicking: false,
    pickGen: 0,
    pendingPick: null,
    petUntil: 0,
    petAt: 0,
    isHushed: false,
    hushedFrame: '',
    isOpening: false,
  }

  on('session.start', async ($, e, next) => {
    const result = await next(e)
    if (e.surface !== 'terminal' || !e.isInteractive) {
      return result
    }
    band.isEnabled = (await $.store.get(STORE_ENABLED).catch(() => undefined)) !== false
    const savedScale = await $.store.get(STORE_SCALE).catch(() => undefined)
    band.scale = SCALES.includes(savedScale as never) ? (savedScale as number) : null
    band.sessionId = await sessionIdOf($)
    // The engine has already drawn the band by now — for the fallback animal,
    // at no saved scale, enabled — and nothing redraws it on its own: switch
    // the art as well as the name, or the fallback walks in and stays until
    // the terminal is next resized (a tmux split was how it showed).
    const animal = await pickFor($, band.sessionId)
    if (animal !== band.animal || buildFor(animal, band.scale) !== band.build) {
      switchTo(band, animal)
    }
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
    // and redraw whatever the store changed: the animal, the scale's layout,
    // or a companion turned off, which draws nothing at all
    $.ui.invalidate('ui.render')
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
        // the frame clock stops with the band: a click from before must not
        // pet the animal once it is back
        band.petUntil = 0
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
      const key = commandOf(animal)
      if (arg === 'random') {
        await $.store.delete(STORE_PIN).catch(() => undefined)
        return { text: `${key}: new sessions pick their own animal from the session id` }
      }
      if (arg === 'never' || arg === 'include') {
        const prefs = await prefsFor($)
        const never = prefs.never.filter(n => n !== key)
        if (arg === 'never') {
          if (ANIMALS.every(a => a === animal || never.includes(commandOf(a)))) {
            return { text: `${key}: it is the last animal in the random pick; include another first` }
          }
          never.push(key)
          if (prefs.pin === key) await $.store.delete(STORE_PIN).catch(() => undefined)
        }
        await $.store.set(STORE_NEVER, never).catch(() => undefined)
        return { text: arg === 'never' ? `${key}: left out of the random pick` : `${key}: back in the random pick` }
      }
      const isAlways = arg === 'always'
      const isDemo = arg !== '' && arg !== 'on' && arg !== 'auto' && !isAlways
      const demoBuild = buildFor(animal, band.scale)
      if (isDemo && !(demoBuild.demo && (arg === 'demo' || (demoBuild.demoPoses ?? []).includes(arg)))) {
        return { text: `usage: /${commandOf(animal)} ${usageOf(animal)}` }
      }
      // this command decides the session's animal: a pick still in flight for
      // it yields, and one the engine's new session id (after a /clear or a
      // /resume) was waiting for is this one
      band.pickGen += 1
      const id = await sessionIdOf($)
      let isPending = false
      if (id && id !== band.sessionId) {
        band.sessionId = id
        band.needsRepick = false
        band.repickUntil = 0
      } else if (band.needsRepick) {
        // typed in the new session before the engine said its id: kept for it,
        // and not written under the old session's id
        band.pendingPick = key
        isPending = true
      }
      const isSwitch = band.animal !== animal || !band.isEnabled
      band.isEnabled = true
      band.animal = animal
      await $.store.set(STORE_ENABLED, true).catch(() => undefined)
      if (band.sessionId && !isPending) {
        const sessions = sessionsOf(await $.store.get(STORE_SESSIONS).catch(() => undefined))
        await $.store.set(STORE_SESSIONS, remember(sessions, band.sessionId, key)).catch(() => undefined)
      }
      if (isAlways) {
        const prefs = await prefsFor($)
        await $.store.set(STORE_PIN, key).catch(() => undefined)
        await $.store.set(STORE_NEVER, prefs.never.filter(n => n !== key)).catch(() => undefined)
      }
      if (isDemo) {
        // another animal's demo plays in that animal's own art, from the start
        band.build = buildFor(animal, band.scale)
        play(band, arg)
      } else if (isSwitch) {
        switchTo(band, animal)
      } else if (band.mode !== 'auto') {
        play(band, 'auto')
      }
      start($, band)
      $.ui.invalidate('ui.render')
      if (isAlways) {
        return { text: `${key}: every new session starts with ${titleOf(animal).toLowerCase()} (\`/${key} random\` undoes it)` }
      }
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
    if (band.needsRepick) void repick($, band)
    return next(e)
  })

  on('turn.start', async ($, e, next) => {
    const result = await next(e)
    band.isWorking = true
    nudge(band)
    if (band.needsRepick) void repick($, band)
    return result
  })

  // A /clear or a /resume goes on under another session id with no
  // session.start: that session shows its own animal, picked once the engine
  // answers with its id.
  on('session.end', async ($, e, next) => {
    const result = await next(e)
    if (e.reason === 'clear' || e.reason === 'resume') {
      band.needsRepick = true
      band.repickUntil = band.frame + REPICK_FRAMES
      void repick($, band)
    }
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
    announce(band, firedOf(result.additionalContext ?? [], result.deny ?? result.ask).map(saidOfFire))
    return result
  })

  on('classic.PostToolUse', async ($, e, next) => {
    const result = await next(e)
    announce(band, firedOf(result.additionalContext ?? [], result.block).map(saidOfFire))
    return result
  })

  // A rule the harness's background fork filed lands on the server minutes
  // after the Stop that launched it, and nothing records it locally. So each
  // Stop asks the server, without holding the Stop, and announces what this
  // session has not announced yet.
  on('classic.Stop', async ($, e, next) => {
    const result = await next(e)
    if (band.isEnabled && e.session_id) void announceProposed($, band, e.session_id)
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
    const ask = askOf(band)
    // A waiting proposal's buttons sit inside its bubble, where Got it goes.
    // Once the animal has said it and gone, they wait on a row of their own
    // under it; that row is kept while one waits, so the drawing never jumps.
    const extra = band.mode === 'auto' && band.asks.length > 0 ? 1 : 0
    const room = Math.max(1, Math.min(e.props.maxRows, band.roomRows ?? e.props.maxRows) - extra)
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
    if (bodyRows > 0 && bodyRows < band.layout.rows + extra && band.roomRows !== bodyRows) {
      band.roomRows = bodyRows
      $.ui.invalidate('ui.render')
    }
    band.lastCells = cellsOf(band) ?? ''
    const raster = Raster({
      key: RASTER_KEY,
      columns: band.layout.columns,
      rows: band.layout.rows,
      cells: band.lastCells,
    })
    const { Button } = $.ui.resolve(e)
    // opens a rule's page in MemHub Studio. A pressable word rather than a
    // link: a terminal without hyperlinks draws a Link or a Markdown link as
    // its text and then its URL, which ran down the bubble past its bottom.
    const opens = (url: string) => () => void openUrl($, band, url)
    // a ♥ just left of where the ground begins, on its row: a click on it pets
    // the animal. No hotkey: a plain Button draws one as `p: ♥`.
    const pet = petAt(build, band.layout)
    const clicks = Box({
      key: 'companion-clicks', position: 'absolute', top: pet.top, left: pet.left,
      children: Button({ key: PET_KEY, plain: true, dimColor: true, label: '♥', onPress: () => onClick(band) }),
    })
    // a proposal's `rule`, once typed, opens the rule in MemHub Studio: a
    // plain Button labelled `rule`, over the word itself, on the bubble's own
    // background, underlined under the pointer
    const word = wordOf(band)
    band.wordKey = keyOfWord(word)
    const linked = word
      ? [Box({
          key: 'rule-word', position: 'absolute', top: word.at.row, left: word.at.col, width: RULE_WORD.length,
          backgroundColor: BUB_BG_HEX,
          children: Button({
            key: 'rule-link', plain: true, label: RULE_WORD, hover: { underline: true }, onPress: opens(word.ask.url),
          }),
        })]
      : []
    const at = buttonsAt(band, build, band.layout)
    band.buttonsKey = keyOfButtons(at)
    if (!ask || !at) {
      return Box({ justifyContent: 'flex-end', children: Box({ children: [raster, clicks, ...linked] }) })
    }
    const footer = at === 'below' ? null : at
    // under the animal, the rule's name opens it (#67's link), as `rule` does
    const name = (width: number) =>
      ask.url
        ? Button({ key: 'rule-name', plain: true, label: nameOf(ask, width), hover: { underline: true }, onPress: opens(ask.url) })
        : Text({ children: nameOf(ask, width) })
    const isDemo = ask === DEMO_ASK
    const answer = (action: 'activate' | 'reject') =>
      isDemo ? () => demoPress($, band, action) : () => void decide($, band, ask, action)
    // plain, so each reads `1: Activate`: the digit is the key to press
    const controls = band.isDeciding
      ? [Text({ color: '#968ca5', children: 'asking MemHub…' })]
      : isDemo && band.demoNote
        ? [Text({ color: '#968ca5', wrap: 'truncate-end', children: band.demoNote })]
        : [
            Button({ key: 'rule-activate', hotkey: '1', plain: true, label: 'Activate', onPress: answer('activate') }),
            Button({ key: 'rule-reject', hotkey: '2', plain: true, label: 'Reject', onPress: answer('reject') }),
            Button({
              key: 'rule-later', hotkey: '3', plain: true, dimColor: true, label: 'Later',
              onPress: isDemo ? () => demoPress($, band, 'later') : () => later($, band),
            }),
          ]
    if (footer) {
      return Box({
        justifyContent: 'flex-end',
        children: Box({
          children: [
            raster,
            clicks,
            ...linked,
            Box({
              position: 'absolute', top: footer.row, left: footer.col, width: footer.width,
              backgroundColor: BUB_BG_HEX, gap: 2, children: controls,
            }),
          ],
        }),
      })
    }
    return Box({
      flexDirection: 'column',
      alignItems: 'flex-end',
      children: [
        Box({ children: [raster, clicks, ...linked] }),
        Box({ key: 'rule-waiting', gap: 2, children: [Text({ dimColor: true, children: 'new rule waiting:' }), name(40), ...controls] }),
      ],
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
  ['on', 'off', 'auto', 'always', 'random', 'never', 'include', 'scale 1-4',
   ...(animal.builds[0]?.demo ? ['demo', ...(animal.builds[0]?.demoPoses ?? [])] : [])].join('|')

function wantOf(band: Band): Want {
  if (band.queue.length > 0) return 'speak'
  return band.isWorking || band.frame < band.activeUntil || isPetting(band) ? 'look' : 'sleep'
}

/** A click is waiting to be answered with a pet. */
const isPetting = (band: Band) => band.frame < band.petUntil

async function sessionIdOf($: EngineInterface): Promise<string> {
  const id = await $.session.id().catch(() => '')
  return typeof id === 'string' ? id : ''
}

async function prefsFor($: EngineInterface): Promise<Prefs> {
  return prefsOf(
    await $.store.get(STORE_PIN).catch(() => undefined),
    await $.store.get(STORE_NEVER).catch(() => undefined),
  )
}

/** The animal a session shows: its own pick, the pin, or its id's hash (selection.ts). */
async function pickFor($: EngineInterface, sessionId: string): Promise<Animal> {
  const sessions = sessionsOf(await $.store.get(STORE_SESSIONS).catch(() => undefined))
  const name = pick(ANIMALS.map(a => a.name), sessionId, sessions, await prefsFor($))
  return ANIMALS.find(a => a.name === name) ?? DEFAULT_ANIMAL
}

/** Another animal: it starts offscreen, not standing where the last one stood. */
function switchTo(band: Band, animal: Animal) {
  band.animal = animal
  band.mode = 'auto'
  band.build = buildFor(animal, band.scale)
  band.seg = segment(band, 'offscreen')
  band.current = { canvas: [] }
}

/**
 * After a /clear or /resume: once the engine reports the new session's id,
 * pick its animal. Until then (the id not yet changed) it is tried again, by
 * the next event, and by the frame clock for a while. A command that picks
 * an animal meanwhile wins.
 */
async function repick($: EngineInterface, band: Band) {
  if (band.isPicking) return
  band.isPicking = true
  const gen = band.pickGen
  try {
    const id = await sessionIdOf($)
    if (!id || id === band.sessionId || gen !== band.pickGen) return
    band.needsRepick = false
    band.repickUntil = 0
    band.sessionId = id
    const pending = band.pendingPick
    band.pendingPick = null
    if (pending) {
      const sessions = sessionsOf(await $.store.get(STORE_SESSIONS).catch(() => undefined))
      await $.store.set(STORE_SESSIONS, remember(sessions, id, pending)).catch(() => undefined)
    }
    const animal = await pickFor($, id)
    if (gen !== band.pickGen) return
    if (animal !== band.animal) {
      switchTo(band, animal)
      $.ui.invalidate('ui.render')
    }
  } finally {
    band.isPicking = false
  }
}

/** A click on the ♥: a pet (see pat). */
function onClick(band: Band) {
  if (!band.isEnabled) return
  pat(band)
}

/**
 * What a click does to the animal. Asleep, it wakes to be petted; looking, it
 * is petted at once; saying a rule, the bubble goes (Got it); presenting a
 * proposal, nothing — that waits for its answer. Anywhere else the pet waits
 * until the animal is free, for a few seconds. A demo takes no pets.
 */
function pat(band: Band) {
  if (band.mode !== 'auto') return
  const name = band.seg.name
  if (name === 'pet') return
  if (name === 'speak') {
    if (band.speakingTone !== 'proposed') band.isHushed = true
    return
  }
  band.petUntil = band.frame + PET_PENDING_FRAMES
  nudge(band)
}

/**
 * A rule's page in the browser, with the platform's opener; one at a time.
 * Where there is none (a remote shell, a container) nothing happens.
 */
async function openUrl($: EngineInterface, band: Band, url: string) {
  if (band.isOpening) return
  band.isOpening = true
  try {
    await openWith($, url)
  } finally {
    band.isOpening = false
  }
}

/** The platform's opener: `open`, else `xdg-open`; where neither works, nothing. */
async function openWith($: EngineInterface, url: string) {
  for (const opener of ['open', 'xdg-open']) {
    const run = await $.process.run([opener, url], { timeoutMs: 5_000 }).catch(() => null)
    if (run && run.exitCode === 0) return
  }
}

/**
 * Where a rule — or, with no id, the rulebook — opens in MemHub Studio, from
 * rule_decide.py's `url`: harness_stop.rule_url(), the Stop notice's own
 * link, against the install's own MemHub. '' when it has none.
 */
async function studioUrlOf($: EngineInterface, ruleId: string, env: string, timeoutMs: number): Promise<string> {
  const root = $.plugin.root.replace(/\/\.claude-plugin\/?$/, '')
  const argv = ['python3', `${root}/scripts/rule_decide.py`, 'url', ...(ruleId ? [ruleId] : [])]
  const run = await $.process.run(env ? [...argv, '--env', env] : argv, { timeoutMs })
  const got = JSON.parse(run.stdout.trim().split('\n').pop() || '{}') as { url?: unknown }
  return typeof got.url === 'string' && /^https:\/\//.test(got.url) ? got.url : ''
}

const saidOfFire = (fire: Fired): Said => ({
  text: fire.isBlocked ? `Blocked: ${fire.rule}` : `Rule fired: ${fire.rule}`,
  tone: fire.isBlocked ? 'blocked' : 'advice',
})

async function proposedFor($: EngineInterface, session: string): Promise<Proposed[]> {
  const root = $.plugin.root.replace(/\/\.claude-plugin\/?$/, '')
  const argv = ['python3', `${root}/scripts/rule_decide.py`, 'proposed', '--session', session]
  const run = await $.process.run(argv, { timeoutMs: 30_000 }).catch(() => null)
  return run ? proposedOf(run.stdout) : []
}

async function announceProposed($: EngineInterface, band: Band, session: string) {
  const fresh = (await proposedFor($, session)).filter(p => !band.announced.has(p.ruleId))
  if (fresh.length === 0) return
  for (const p of fresh) band.announced.add(p.ruleId)
  announce(band, fresh.map(p => ({ text: proposalSaid(p), tone: 'proposed' as const })))
  band.asks.push(...fresh)
  for (const p of fresh) void linkOf($, p)
  $.ui.invalidate('ui.render')
}

/**
 * Answer the proposal on the buttons through scripts/rule_decide.py — Studio's
 * own PATCH, sent with the plugin's access key — then say how it went. The
 * buttons go the moment it is sent, so a second press cannot answer twice.
 */
async function decide($: EngineInterface, band: Band, ask: Proposed, action: 'activate' | 'reject') {
  if (band.isDeciding || band.asks[0] !== ask) return
  band.isDeciding = true
  $.ui.invalidate('ui.render')
  let d: Decision = { outcome: 'error' }
  try {
    const root = $.plugin.root.replace(/\/\.claude-plugin\/?$/, '')
    const argv = ['python3', `${root}/scripts/rule_decide.py`, ask.ruleId, action]
    const run = await $.process.run(ask.env ? [...argv, '--env', ask.env] : argv, { timeoutMs: 30_000 })
    d = JSON.parse(run.stdout.trim().split('\n').pop() || '{}') as Decision
  } catch (err) {
    d = { outcome: 'error', msg: err instanceof Error ? err.name : 'failed' }
  } finally {
    band.isDeciding = false
    band.asks = band.asks.filter(a => a !== ask)
  }
  announce(band, [{ text: decisionSaid(ask, action, d), tone: 'advice' }])
  $.ui.invalidate('ui.render')
}

/** What the demo's proposal is; it answers nothing, so it needs no id. */
const DEMO_ASK: Proposed = { title: 'Pin the MCP server when spawning claude -p', ruleId: '', env: '' }

/**
 * The proposal the buttons answer: the first one waiting, following the
 * session; or, in `/goose demo` and `/goose propose`, the demo's own while its
 * bubble is a proposal — so the demo shows the whole ask, buttons included.
 */
function askOf(band: Band): Proposed | undefined {
  if (band.mode === 'auto') return band.asks[0]
  return band.hasDemoAsk ? DEMO_ASK : undefined
}

type ButtonsAt = { row: number; col: number; width: number } | 'below' | null

/**
 * Where a waiting proposal's buttons go: inside its bubble, on the row Got it
 * takes, once the text is typed out; on a row under the animal once it has
 * said it and is not about to again; and nowhere while it is still being
 * presented, so they never show under the animal and then jump into the bubble.
 */
function buttonsAt(band: Band, build: Build, layout: Layout): ButtonsAt {
  if (!askOf(band)) return null
  const footer = footerOf(band.current, build, layout)
  if (footer) return footer
  if (band.mode !== 'auto') return null
  const presenting =
    band.queue.some(q => q.tone === 'proposed') ||
    (band.seg.name === 'speak' && band.speakingTone === 'proposed')
  return presenting ? null : 'below'
}

/**
 * The proposal the bubble is saying now, and where its `rule` is, once typed
 * out and when there is a Studio page to link to: in the demo its own, else
 * the waiting proposal whose words these are.
 */
function wordOf(band: Band): { ask: Proposed & { url: string }; at: { row: number; col: number } } | null {
  const { bubble } = band.current
  if (!band.layout || bubble?.tone !== 'proposed') return null
  const ask = band.mode === 'auto'
    ? band.asks.find(a => fitBubble(proposalSaid(a)) === bubble.text)
    : band.hasDemoAsk ? DEMO_ASK : undefined
  if (!ask?.url) return null
  const at = wordAt(band.current, band.build, band.layout, RULE_WORD)
  return at ? { ask: ask as Proposed & { url: string }, at } : null
}

const keyOfWord = (word: ReturnType<typeof wordOf>) => (word ? `${word.at.row},${word.at.col},${word.ask.url}` : '')

const keyOfButtons = (at: ButtonsAt) => (at === null ? '' : at === 'below' ? 'below' : `${at.row},${at.col}`)

/** Short enough for the bubble's footer row, where the buttons were. */
const DEMO_SAYS: Record<'activate' | 'reject' | 'later', string> = {
  activate: 'demo: would turn it on (admins only)',
  reject: 'demo: would dismiss it; never fires',
  later: 'demo: hides these; waits in Studio',
}
/** How long the demo's note stands in for the buttons. */
const DEMO_NOTE_FRAMES = 2 * FPS

/** A press in the demo says what it would do; nothing reaches MemHub. */
function demoPress($: EngineInterface, band: Band, action: keyof typeof DEMO_SAYS) {
  band.demoNote = DEMO_SAYS[action]
  band.demoNoteUntil = band.frame + DEMO_NOTE_FRAMES
  $.ui.invalidate('ui.render')
}

/**
 * Where the rule opens in MemHub Studio, from rule_decide.py's `url` — which
 * is harness_stop.rule_url(), the Stop notice's own link, so the two agree —
 * then a redraw, so the name turns into a link. '' when it has none.
 */
async function linkOf($: EngineInterface, ask: Proposed) {
  if (ask.url !== undefined) return
  ask.url = ''
  try {
    ask.url = await studioUrlOf($, ask.ruleId, ask.env, 15_000)
  } catch {
    ask.url = ''
  }
  $.ui.invalidate('ui.render')
}

/** Later: the buttons go; the rule stays proposed, in Studio. */
function later($: EngineInterface, band: Band) {
  band.asks.shift()
  $.ui.invalidate('ui.render')
}

function announce(band: Band, said: readonly Said[]) {
  if (said.length === 0) return
  band.queue.push(...said)
  band.queue.splice(0, Math.max(0, band.queue.length - MAX_QUEUED))
  nudge(band)
}

function segment(band: Band, name: PoseName): Segment {
  const { poses } = band.build
  band.isHushed = false
  band.hushedFrame = ''
  switch (name) {
    case 'offscreen': return { name, frames: [][Symbol.iterator]() }
    case 'enter': return { name, frames: poses.enter() }
    case 'sleep': return { name, frames: poses.sleep() }
    case 'wake': return { name, frames: poses.wake() }
    case 'look': return { name, frames: poses.look() }
    case 'rise': return { name, frames: poses.rise() }
    case 'leave': return { name, frames: poses.leave() }
    case 'demo': return { name, frames: band.build.demo!(band.mode === 'demo' ? null : band.mode) }
    case 'pet': {
      band.petUntil = 0
      band.petAt = band.frame
      // a petted animal stays awake a while, then dozes off as after a turn
      band.activeUntil = band.frame + LINGER_FRAMES
      return { name, frames: poses.pet?.() ?? lookAround(poses.look(), FALLBACK_PET_FRAMES) }
    }
    case 'speak': {
      const said = band.queue.shift()!
      band.fires += 1
      band.speakingTone = said.tone
      return { name, frames: poses.speak(fitBubble(said.text), band.fires, said.tone) }
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
    case 'wake': return want === 'speak' ? 'rise' : isPetting(band) ? 'pet' : want === 'look' ? 'look' : 'sleep'
    case 'pet': return want === 'speak' ? 'rise' : 'look'
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
  } else if (name === 'look' && isPetting(band)) {
    band.seg = segment(band, 'pet')
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
    if (r.done) {
      band.seg = segment(band, after(band, band.seg.name))
      continue
    }
    if (band.isHushed) {
      // Got it: the motion plays on, the holds are skipped
      const seen = JSON.stringify(r.value)
      if (seen === band.hushedFrame) continue
      band.hushedFrame = seen
    }
    return r.value
  }
}

/**
 * `n` frames of an endless `look`, the last of them its first again, so the
 * look that follows starts where this one ends — the ring's rule for a pet.
 */
function* lookAround<F>(frames: Iterator<F>, n: number): Generator<F> {
  const first = frames.next()
  if (first.done) return
  yield first.value
  for (let i = 1; i < n - 1; i++) {
    const r = frames.next()
    if (r.done) break
    yield r.value
  }
  yield first.value
}

/** The director's own pet: two hearts rising over the animal, two canvas rows every 8 frames. */
function withHearts(painted: Painted, build: Build, age: number): Painted {
  const last = build.size.columns - 1
  const x = Math.min(last, build.bubbleAt.column + 4)
  const y = Math.max(0, build.bubbleAt.row - 6 - 2 * Math.floor(age / 8))
  const hearts = [
    { x, y, ch: '♥', color: HEART_RGB },
    { x: Math.min(last, x + 3), y: Math.min(build.size.rows - 1, y + 2), ch: '♥', color: HEART_RGB },
  ]
  return { ...painted, glyphs: [...(painted.glyphs ?? []), ...hearts] }
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
  if (band.needsRepick && band.repickUntil > band.frame && band.frame % 10 === 0) {
    void repick($, band)
  }
  band.current = band.build.render(advance(band), band.frame)
  if (band.isHushed) {
    band.current = { ...band.current, bubble: null }
  }
  if (band.seg.name === 'pet' && !band.build.poses.pet) {
    band.current = withHearts(band.current, band.build, band.frame - band.petAt)
  }
  // the demo's buttons come and go with its proposal bubble; a blit cannot
  // draw them, so a change asks for a render
  const hasDemoAsk = band.mode !== 'auto' && band.current.bubble?.tone === 'proposed'
  if (hasDemoAsk !== band.hasDemoAsk) {
    band.hasDemoAsk = hasDemoAsk
    band.demoNote = null
    if (hasDemoAsk) void linkOf($, DEMO_ASK)
    $.ui.invalidate('ui.render')
  }
  if (band.demoNote && band.frame >= band.demoNoteUntil) {
    band.demoNote = null
    $.ui.invalidate('ui.render')
  }
  // the buttons ride the proposal bubble's footer: when they appear, move or
  // go, the tree has to be drawn again, which a blit cannot do
  const at = band.layout ? buttonsAt(band, band.build, band.layout) : null
  if (keyOfButtons(at) !== band.buttonsKey || keyOfWord(wordOf(band)) !== band.wordKey) {
    $.ui.invalidate('ui.render')
  }
  const cells = cellsOf(band)
  band.frame += 1
  const { requestId } = band
  if (requestId && cells && cells !== band.lastCells) {
    band.lastCells = cells
    await $.ui.blit({ requestId, key: RASTER_KEY, cells }).catch(() => undefined)
  }
}
