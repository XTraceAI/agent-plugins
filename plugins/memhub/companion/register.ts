// One entry point: mod/register.ts, the plugin's hooks module (hooks.json),
// calls `register(on, ctx)` with the mod's Ctx (mod/ctx.ts), last of its
// parts, so the companion's hooks sit innermost. MemHub calls use
// `ctx.api()`: the REST base, the bearer, and the Studio web app paired with
// the base; `ctx.env` says which MemHub it is.
//
// It reads what the mod writes to `$.state` (fires, proposals,
// lanes) and never needs the classic.* events, which the built-in
// `sec-default` guard skips for user mods on Team/Enterprise sign-ins (Claude
// Mods Migration Spec §1.4, D3). The classic PreToolUse / PostToolUse hooks
// stay only as the fallback for lanes the mod has not claimed.

import type { EngineInterface, On, Timer } from 'claude-code'
import { read, update } from 'claude-code'

import type { Api, Ctx } from '../mod/ctx'
import type { FireNote, ProposalNote } from '../types'
import type { Animal, Build, Painted, Tone } from './animal'
import { ANIMALS, DEFAULT_ANIMAL } from './animals'
import {
  decisionOf, envName, firesOf, freshFires, headersOf, isApi, isClassicLane, lanesOf, listedRules,
  mergedProposals, PROPOSED_PATH, proposalsKey, proposalsOf, proposedOfNote, rulePath, STATUS_OF, studioUrl, UUID,
  versionOfManifest,
} from './feed'
import {
  announcedIn, announcedOf, type Decision, decisionSaid, nameOf, type Proposed, proposalSaid, RULE_WORD,
  withAnnounced,
} from './proposed'
import { type Fired, firedOf } from './rules'
import { BUB_BG_HEX, CELL_H, CELL_W, compose, DESKTOP_TEXT_LINES, encode, fitBubble, footerOf, type Layout, layoutOf, petAt, SCALES, svgOf, wordAt } from './screen'
import { pick, poolOf, prefsOf, type Prefs, remember, sessionsOf } from './selection'

/** What the mod's act side writes on each fire (types/index.d.ts). */
const FIRES = { plugin: 'memhub', key: 'fires' } as const
/** Rules waiting on an answer: the poll below writes them. */
const PROPOSALS = { plugin: 'memhub', key: 'proposals' } as const
/** The Rulebook lanes the mod serves now; the classic hooks announce the rest. */
const LANES = { plugin: 'memhub', key: 'lanes' } as const
/** How often the server is asked for proposed rules, besides session start and each turn's end. */
const POLL_MS = 300_000

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
 *
 * The terminal draws the band as a Raster, repainted in place by `$.ui.blit`.
 * The desktop lists a Raster but draws nothing for one and refuses its blits,
 * so there the same cells are drawn as an SVG and each changed frame redraws
 * the band; its buttons sit in a row of their own, since a Box is placed in
 * cells and the SVG is sized in CSS pixels, which need not agree.
 */

const FPS = 20
/** The desktop redraws the band for a frame, not a blit: every frame that changed. */
const DESKTOP_STRIDE = 1
const RASTER_KEY = 'companion'
const STORE_ENABLED = 'companion.enabled'
// `companion.animal`, the old global pick, is neither read nor written: most
// of it was `/goose` run to turn the band on, not a choice. Older versions
// still read it, so it is left where it is.
const STORE_PIN = 'companion.pin'
const STORE_NEVER = 'companion.never'
const STORE_SESSIONS = 'companion.sessions'
const STORE_SCALE = 'companion.scale'
/** Per session, the proposed rules already announced: a reload must not announce them again. */
const STORE_ANNOUNCED = 'companion.announced'
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
  /**
   * Rule ids already announced: the server lists a proposal until it is
   * decided. Kept in the store too (STORE_ANNOUNCED), per session, since a
   * reload of this module starts this set empty.
   */
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
  /** Where the band was last drawn: the terminal blits a frame, the desktop redraws it. */
  surface: 'terminal' | 'desktop'
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
  /** setUp has run (or is running) for this load of the module. */
  isSetUp: boolean
  /** The mod's Ctx, from mod/register.ts: the credential and the env. */
  ctx: Ctx
  /**
   * The `$.state` fires already seen, by feed.fireKey; null until the first
   * read of this load, which takes what is there as said (a reload must not
   * say a session's fires again).
   */
  seenFires: Set<string> | null
  /** The proposals last handed to the band (feed.proposalsKey); null before any. */
  proposalsKey: string | null
  /** Proposal updates, one after another, so two never announce the same rule. */
  syncing: Promise<void>
  pollTimer: Timer | null
  isPolling: boolean
  /** The MemHub the poll reached ('staging' | 'production'); '' until it has. */
  env: string
}

export function register(on: On, ctx: Ctx) {
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
    surface: 'terminal',
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
    isSetUp: false,
    ctx,
    seenFires: null,
    proposalsKey: null,
    syncing: Promise.resolve(),
    pollTimer: null,
    isPolling: false,
    env: '',
  }

  on('session.start', async ($, e, next) => {
    const result = await next(e)
    // `claude -p` is a terminal that is not interactive, with no band to draw
    // in. The desktop reports no surface here and `isInteractive: false` (a
    // live 2.1.287 session), so it sets up from its first draw instead.
    if (e.surface === 'terminal' && e.isInteractive) {
      await setUp($, band)
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

  // A rule the harness's background fork filed lands on the server minutes
  // after the turn that launched it. Besides the clock (setUp), each main
  // turn's end asks the server, without holding the turn — as each classic
  // Stop used to, which the guard skips for most customers (D3).
  on('turn.complete', async ($, e, next) => {
    const result = await next(e)
    if (e.agentId === undefined) {
      band.isWorking = false
      band.activeUntil = band.frame + LINGER_FRAMES
      if (band.isSetUp) void pollProposals($, band)
    }
    return result
  })

  // The fallback for fires: the rulebook's command hooks sit beneath every
  // hooks module in the classic chain, so `next(e)` is what they answered for
  // this call. Read only while the mod has not claimed that lane: a claimed
  // lane's fires come through `$.state` fires (see ui.render), and the Python
  // hook it suppresses prints nothing here anyway. Where the built-in guard
  // skips classic.* for user mods, these never run at all.
  on('classic.PreToolUse', async ($, e, next) => {
    const result = await next(e)
    if (isClassicLane(await lanesNow($), 'pre')) {
      announce(band, firedOf(result.additionalContext ?? [], result.deny ?? result.ask).map(saidOfFire))
    }
    return result
  })

  on('classic.PostToolUse', async ($, e, next) => {
    const result = await next(e)
    if (isClassicLane(await lanesNow($), 'post')) {
      announce(band, firedOf(result.additionalContext ?? [], result.block).map(saidOfFire))
    }
    return result
  })

  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    // Read while drawing, so a write to either redraws the band and lands here.
    await feedFromState($, band)
    if (!band.isEnabled || e.props.hasSurvey || (e.surface !== 'terminal' && e.surface !== 'desktop')) {
      band.requestId = null
      return next(e)
    }
    const { Box, Text, Button } = $.ui.resolve(e)
    band.requestId = e.requestId
    band.surface = e.surface
    if (e.surface === 'desktop') void setUp($, band)
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
    // On the desktop they always have that row (see the header).
    const isRowed = e.surface === 'desktop' ? ask !== undefined : band.mode === 'auto' && band.asks.length > 0
    const extra = isRowed ? 1 : 0
    const room = Math.max(1, Math.min(e.props.maxRows, band.roomRows ?? e.props.maxRows) - extra)
    const build = buildFor(band.animal, band.scale)
    if (build !== band.build) {
      // different art, different frames: it starts over rather than cutting
      // from one drawing's pose into another's
      band.build = build
      band.seg = segment(band, 'offscreen')
      band.current = { canvas: [] }
    }
    band.layout = layoutOf(build, e.props.bodyColumns, room, band.scale ?? undefined, linesOf(band), e.surface === 'desktop' ? 0 : 1)
    if (!band.layout) {
      const name = band.animal.name.toLowerCase()
      const where = e.surface === 'desktop' ? 'window' : 'terminal'
      return Text({ dimColor: true, children: `make the ${where} a little bigger for the ${name}` })
    }
    const { bodyRows } = e.props.scroll
    if (bodyRows > 0 && bodyRows < band.layout.rows + extra && band.roomRows !== bodyRows) {
      band.roomRows = bodyRows
      $.ui.invalidate('ui.render')
    }
    // opens a rule's page in MemHub Studio. A pressable word rather than a
    // link: a terminal without hyperlinks draws a Link or a Markdown link as
    // its text and then its URL, which ran down the bubble past its bottom.
    const opens = (url: string) => () => void openUrl($, band, url)
    const petButton = () =>
      Button({ key: PET_KEY, plain: true, dimColor: true, label: '♥', onPress: () => onClick(band) })
    const at = buttonsAt(band, build, band.layout)
    band.buttonsKey = keyOfButtons(at)
    // under the animal, the rule's name opens it (#67's link), as `rule` does
    const name = (ask: Proposed, width: number) =>
      ask.url
        ? Button({ key: 'rule-name', plain: true, label: nameOf(ask, width), hover: { underline: true }, onPress: opens(ask.url) })
        : Text({ children: nameOf(ask, width) })
    const controlsFor = (ask: Proposed) => {
      const isDemo = ask === DEMO_ASK
      const answer = (action: 'activate' | 'reject') =>
        isDemo ? () => demoPress($, band, action) : () => void decide($, band, ask, action)
      // plain, so each reads `1: Activate`: the digit is the key to press
      return band.isDeciding
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
    }
    const waiting = (ask: Proposed) =>
      Box({ key: 'rule-waiting', gap: 2, children: [Text({ dimColor: true, children: 'new rule waiting:' }), name(ask, 40), ...controlsFor(ask)] })

    if (e.surface === 'desktop') {
      const buf = compose(band.current, build, titleOf(band.animal), band.layout)
      band.lastCells = encode(buf)
      band.wordKey = ''
      const art = Box({
        alignItems: 'flex-end',
        children: [
          petButton(),
          // no `key`: an Svg takes none (SvgProps), and Claude Code 2.1.292
          // drops one from the drawn tree
          $.ui.resolve(e).Svg({
            source: svgOf(buf),
            alt: `${titleOf(band.animal)}, the MemHub companion`,
            width: band.layout.columns * CELL_W,
            height: band.layout.rows * CELL_H,
          }),
        ],
      })
      if (!ask || at === null) return Box({ justifyContent: 'flex-end', children: art })
      return Box({ flexDirection: 'column', alignItems: 'flex-end', children: [art, waiting(ask)] })
    }

    band.lastCells = cellsOf(band) ?? ''
    const raster = $.ui.resolve(e).Raster({
      key: RASTER_KEY,
      columns: band.layout.columns,
      rows: band.layout.rows,
      cells: band.lastCells,
    })
    // a ♥ just left of where the ground begins, on its row: a click on it pets
    // the animal. No hotkey: a plain Button draws one as `p: ♥`.
    const pet = petAt(build, band.layout)
    const clicks = Box({ key: 'companion-clicks', position: 'absolute', top: pet.top, left: pet.left, children: petButton() })
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
    if (!ask || !at) {
      return Box({ justifyContent: 'flex-end', children: Box({ children: [raster, clicks, ...linked] }) })
    }
    if (at !== 'below') {
      return Box({
        justifyContent: 'flex-end',
        children: Box({
          children: [
            raster,
            clicks,
            ...linked,
            Box({
              position: 'absolute', top: at.row, left: at.col, width: at.width,
              backgroundColor: BUB_BG_HEX, gap: 2, children: controlsFor(ask),
            }),
          ],
        }),
      })
    }
    return Box({
      flexDirection: 'column',
      alignItems: 'flex-end',
      children: [Box({ children: [raster, clicks, ...linked] }), waiting(ask)],
    })
  })
}

/**
 * What the mod wrote to `$.state` since the band last looked: each new fire
 * announced once (crossly when it blocked the call), and the proposals handed
 * to the band when their list changed. Called while drawing, so the reads
 * subscribe the band and every write draws it again. A read that fails is
 * nothing new.
 */
async function feedFromState($: EngineInterface, band: Band) {
  const fires = firesOf(await readSafe(() => read($, FIRES)))
  const { fresh, seen } = freshFires(fires, band.seenFires ?? new Set())
  const isFirstLook = band.seenFires === null
  band.seenFires = seen
  // the first look of this load takes what is there as already said
  if (!isFirstLook && band.isEnabled) announce(band, fresh.map(saidOfNote))
  const notes = proposalsOf(await readSafe(() => read($, PROPOSALS)))
  if (notes !== null && proposalsKey(notes) !== band.proposalsKey) {
    band.proposalsKey = proposalsKey(notes)
    void syncProposals($, band, notes)
  }
}

/** A `$.state` read that cannot throw: undefined for anything that went wrong. */
async function readSafe(get: () => Promise<unknown>): Promise<unknown> {
  try {
    return await get()
  } catch {
    return undefined
  }
}

/** The Rulebook lanes the mod has claimed; none when unknown. */
async function lanesNow($: EngineInterface) {
  return lanesOf(await readSafe(() => read($, LANES)))
}

/**
 * Ask the server for this session's proposed rules — once at a time — merge
 * its list into `$.state` proposals and hand the result to the band. A poll
 * that could not ask changes nothing.
 */
async function pollProposals($: EngineInterface, band: Band) {
  if (band.isPolling || !band.isEnabled) return
  band.isPolling = true
  try {
    const session = await sessionIdOf($)
    if (!session) return
    const since = await $.clock.now().catch(() => 0)
    const listed = await listProposed($, band, session, since)
    if (!listed) return
    band.env = envName(band.ctx.env)
    const merged = await update($, PROPOSALS, current => mergedProposals(proposalsOf(current) ?? [], listed.notes, since))
      .then(proposalsOf, () => null)
    // no $.state to write to (or the write refused): the server's list is the list
    const notes = merged ?? listed.notes
    band.proposalsKey = proposalsKey(notes)
    await syncProposals($, band, notes)
  } catch {
    // companion-upgrades §1.2: never thrown out of a timer
  } finally {
    band.isPolling = false
  }
}

/** Hand `notes` to the band after any update still running. */
function syncProposals($: EngineInterface, band: Band, notes: readonly ProposalNote[]): Promise<void> {
  band.syncing = band.syncing.then(() => applyProposals($, band, notes)).catch(() => undefined)
  return band.syncing
}

/**
 * `notes` is the whole of what is still waiting: an ask on the band it no
 * longer names (decided in Studio, or by anyone else) comes off, buttons and
 * any queued saying of it with it; a rule this session has not announced yet
 * is said and asked. What was announced is kept in the store per session, so
 * a reload of this module does not say it again (companion-upgrades §3.11).
 */
async function applyProposals($: EngineInterface, band: Band, notes: readonly ProposalNote[]) {
  const waiting = new Set(notes.map(p => p.ruleId))
  const gone = band.asks.filter(a => !waiting.has(a.ruleId))
  if (gone.length > 0) {
    band.asks = band.asks.filter(a => waiting.has(a.ruleId))
    const unsaid = new Set(gone.map(proposalSaid))
    band.queue = band.queue.filter(q => !(q.tone === 'proposed' && unsaid.has(q.text)))
    $.ui.invalidate('ui.render')
  }
  if (!band.isEnabled) return
  const session = await sessionIdOf($)
  const stored = announcedOf(await $.store.get(STORE_ANNOUNCED).catch(() => undefined))
  if (session) for (const id of announcedIn(stored, session)) band.announced.add(id)
  const fresh = notes.filter(p => !band.announced.has(p.ruleId)).map(p => proposedOfNote(p, band.env))
  if (fresh.length === 0) return
  for (const p of fresh) band.announced.add(p.ruleId)
  if (session) {
    await $.store.set(STORE_ANNOUNCED, withAnnounced(stored, session, fresh.map(p => p.ruleId))).catch(() => undefined)
  }
  announce(band, fresh.map(p => ({ text: proposalSaid(p), tone: 'proposed' as const })))
  band.asks.push(...fresh)
  for (const p of fresh) void linkOf($, band, p)
  $.ui.invalidate('ui.render')
}

// MemHub over `$.http.fetch` (spec §4.11, W12): the proposal poll, Activate /
// Reject, and where a rule opens in Studio. They used to shell out to
// `python3 scripts/rule_decide.py` per call; that script stays for the
// skills, and its requests are mirrored here (feed.ts). Every call settles:
// a failure is an answer (null, 'no_key', an error Decision), never a throw
// (companion-upgrades §1.2). These live in this file because `claude plugin
// validate` follows `$` into no imported function.

/**
 * The REST base, bearer and Studio origin: the mod's `ctx.api()`, which runs
 * `rulebook_mod_cli.py api-info` once a session and keeps the answer in module
 * memory only (a secret: never `$.state` or `$.store`).
 */
async function apiFor(band: Band): Promise<Api | undefined> {
  return band.ctx.api().then(a => (isApi(a) ? a : undefined), () => undefined)
}

/** Drop the cached credential (after a 401) so the next apiFor() resolves it again. */
function forgetApi(band: Band) {
  try {
    band.ctx.forgetApi()
  } catch {
    // a ctx that cannot forget costs one more 401, never a thrown hook
  }
}

/** The plugin's folder: `$.plugin.root` may name its `.claude-plugin`. */
function rootOf($: EngineInterface) {
  return $.plugin.root.replace(/\/\.claude-plugin\/?$/, '')
}

let manifestVersion: Promise<string> | undefined

/** plugin_version.request_headers()'s value: the manifest's version, '' when unreadable. */
function versionOf($: EngineInterface): Promise<string> {
  manifestVersion ??= $.fs.read(`${rootOf($)}/.claude-plugin/plugin.json`).then(versionOfManifest, () => '')
  return manifestVersion
}

type Sent = { status: number; text: string; base: string }

/**
 * One request to MemHub's REST API with the plugin's key, as rule_decide.py
 * sends it. A 401 drops the cached key and tries once more with a fresh one.
 * 'no_key' when there is no key to send; null when no answer came.
 */
async function send($: EngineInterface, band: Band, path: string, body?: { method: string; body: string }): Promise<Sent | 'no_key' | null> {
  for (let attempt = 0; ; attempt += 1) {
    const api = await apiFor(band).catch(() => undefined)
    if (!api) return 'no_key'
    const headers = headersOf(api, await versionOf($), body !== undefined)
    const base = api.base.replace(/\/+$/, '')
    const res = await $.http.fetch(`${base}${path}`, { ...body, headers }).catch(() => null)
    if (!res) return null
    if (res.status === 401 && attempt === 0) {
      forgetApi(band)
      continue
    }
    return { status: res.status, text: typeof res.text === 'string' ? res.text : '', base }
  }
}

/**
 * This session's rules still `proposed` as the server lists them, and the
 * base that answered; null when it could not be asked or answered oddly —
 * then nothing may be taken off the band.
 */
async function listProposed($: EngineInterface, band: Band, session: string, at: number): Promise<{ notes: ProposalNote[]; base: string } | null> {
  const sent = await send($, band, PROPOSED_PATH)
  if (!sent || sent === 'no_key' || sent.status < 200 || sent.status >= 300) return null
  const notes = listedRules(sent.text, session, at)
  return notes ? { notes, base: sent.base } : null
}

/** rule_decide.py `decide()`: Studio's own PATCH, and what came of it. */
async function decideRule($: EngineInterface, band: Band, ruleId: string, action: keyof typeof STATUS_OF): Promise<Decision> {
  if (!UUID.test(ruleId)) return { outcome: 'error', msg: 'not a rule id' }
  const sent = await send($, band, rulePath(ruleId), { method: 'PATCH', body: JSON.stringify({ status: STATUS_OF[action] }) })
  if (sent === 'no_key') return { outcome: 'no_key', msg: 'no stored access key; run /memhub:login' }
  if (!sent) return { outcome: 'error', msg: 'no connection' }
  // a second 401, with a freshly resolved key: the key itself is refused
  if (sent.status === 401) return { outcome: 'no_key', msg: 'access key refused' }
  return decisionOf(sent.status, sent.text, action)
}

/** Where the rule (or, with no id, the rulebook) opens in MemHub Studio; '' for nowhere. */
async function studioUrlFor($: EngineInterface, band: Band, ruleId: string): Promise<string> {
  const api = await apiFor(band).catch(() => undefined)
  return api ? studioUrl(api.studio ?? '', ruleId) : ''
}

/** Take a decided rule out of `$.state` proposals, so no reader asks it again. */
async function forget($: EngineInterface, band: Band, ruleId: string) {
  const left = await update($, PROPOSALS, current => (proposalsOf(current) ?? []).filter(p => p.ruleId !== ruleId))
    .then(proposalsOf, () => null)
  if (left) band.proposalsKey = proposalsKey(left)
}

/**
 * The companion's start, once a session: its saved state, its animal, its
 * commands and its frame clock. The terminal runs it at session.start; the
 * desktop, whose session.start says nothing of where it draws, at its first
 * draw of the band.
 */
async function setUp($: EngineInterface, band: Band) {
  if (band.isSetUp) return
  band.isSetUp = true
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
  // proposed rules: once now, then on the clock (and at each turn's end)
  band.pollTimer ??= $.clock.every(POLL_MS, () => void pollProposals($, band))
  void pollProposals($, band)
}

/** The most lines a bubble says where the band is drawn; the terminal's default when undefined. */
const linesOf = (band: Band) => (band.surface === 'desktop' ? DESKTOP_TEXT_LINES : undefined)

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

const saidOfFire = (fire: Fired): Said => ({
  text: fire.isBlocked ? `Blocked: ${fire.rule}` : `Rule fired: ${fire.rule}`,
  tone: fire.isBlocked ? 'blocked' : 'advice',
})

/** A `$.state` fire as the animal says it: the same words as one read off the classic answer. */
const saidOfNote = (note: FireNote): Said => saidOfFire({ rule: note.rule.replace(/\s+/g, ' ').trim(), isBlocked: note.isBlocked })

/**
 * Answer the proposal on the buttons with Studio's own PATCH, sent with the
 * plugin's access key (decideRule: rule_decide.py's request), then say how it
 * went. The buttons go the moment it is sent, so a second press cannot
 * answer twice; a rule that left `proposed` leaves `$.state` proposals too.
 */
async function decide($: EngineInterface, band: Band, ask: Proposed, action: 'activate' | 'reject') {
  if (band.isDeciding || band.asks[0] !== ask) return
  band.isDeciding = true
  $.ui.invalidate('ui.render')
  let d: Decision = { outcome: 'error' }
  try {
    d = await decideRule($, band, ask.ruleId, action)
  } catch (err) {
    d = { outcome: 'error', msg: err instanceof Error ? err.name : 'failed' }
  } finally {
    band.isDeciding = false
    band.asks = band.asks.filter(a => a !== ask)
  }
  if (d.outcome === 'active' || d.outcome === 'dismissed' || d.outcome === 'decided' || d.outcome === 'gone') {
    await forget($, band, ask.ruleId).catch(() => undefined)
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
    ? band.asks.find(a => fitBubble(proposalSaid(a), linesOf(band)) === bubble.text)
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
 * Where the rule opens in MemHub Studio — the web app api-info pairs with the
 * API the plugin reaches (plugin_onboarding._ORIGINS, which harness_stop's
 * rule_url() uses too, so it agrees with the Stop notice's link) — then a
 * redraw, so the name turns into a link. '' when it has none.
 */
async function linkOf($: EngineInterface, band: Band, ask: Proposed) {
  if (ask.url !== undefined) return
  ask.url = ''
  try {
    ask.url = await studioUrlFor($, band, ask.ruleId)
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
      return { name, frames: poses.speak(fitBubble(said.text, linesOf(band)), band.fires, said.tone) }
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
    if (hasDemoAsk) void linkOf($, band, DEMO_ASK)
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
  if (!requestId || !cells || cells === band.lastCells) return
  if (band.surface === 'desktop') {
    // the render draws the frame and records it; one skipped here is drawn
    // by the next, since its cells still differ from the last drawn
    if (band.frame % DESKTOP_STRIDE === 0) $.ui.invalidate('ui.render')
    return
  }
  band.lastCells = cells
  await $.ui.blit({ requestId, key: RASTER_KEY, cells }).catch(() => undefined)
}
