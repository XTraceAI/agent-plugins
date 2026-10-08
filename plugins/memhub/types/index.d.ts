// The plugin's $.state contract: every value the mod keeps for the session,
// declared once. `claude plugin validate` holds each `$.state` key the module
// names to this file. Writers and readers are named per key.

/** One rule that fired on one call, as the person and the companion see it. */
export type FireNote = {
  /** The rule's id on the server. */
  ruleId: string
  /** rulebook_hook's `disclosure_line()` text, byte for byte. */
  line: string
  /** The rule in 20 words or fewer (the line without its marker). */
  rule: string
  /** True when the call was refused by it. */
  isBlocked: boolean
  /** ms since epoch. */
  at: number
}

/** A rule the harness filed as `proposed`, awaiting Activate / Reject / Later. */
export type ProposalNote = {
  ruleId: string
  title: string
  rulebookId?: string
  /** Who it applies to, as the server labels its scope ("Everyone in Acme", "Just you"). */
  audience?: string
  at: number
}

/** Which Rulebook lanes the mod serves this session (spec §3.3). */
export type LaneName = 'pre' | 'post' | 'prompt' | 'session'

declare module 'claude-code' {
  interface PluginState {
    'memhub': {
      /** Writer: mod/act.ts. Reader: the companion. Newest last, capped at 50. */
      fires: FireNote[]
      /** Writer: the companion's proposal poll (and its own answers). Reader: the companion. */
      proposals: ProposalNote[]
      /** Writer: mod/claims.ts. The lanes claimed right now. */
      lanes: LaneName[]
      /** Writer: any lane that hit an error the person should know of; '' when healthy. */
      health: string
    }
  }
}
