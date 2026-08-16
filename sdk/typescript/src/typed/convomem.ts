// GENERATED — do not edit by hand.
//
// Regenerate with `npm run codegen` against the target Dograh backend.
// Source of truth: the backend's model-backed node-spec catalog served
// from `/api/v1/node-types`.


/**
 * Give the agent long-term memory of each caller via ConvoMem
 *
 * LLM hint: ConvoMem is a customer-memory layer. It enriches the call before it starts and stores the transcript after it ends. It does not participate in the conversation graph and should not be connected to other nodes.
 */
export interface Convomem {
    type: "convomem";
    /**
     * Short identifier for this ConvoMem configuration.
     */
    name?: string;
    /**
     * When false, Dograh skips ConvoMem entirely for this call.
     */
    convomem_enabled?: boolean;
    /**
     * Your ConvoMem org API key (starts with sk-org-). Scope it to a single agent so recall and capture land under the right agent.
     */
    convomem_api_key: string;
    /**
     * Also stream the transcript to ConvoMem *during* the call, fire-and-forget, so memory is fresh mid-call. The end-of-call capture still runs as the source of truth. Off by default.
     */
    convomem_live_capture?: boolean;
    /**
     * Optional cap on how much recalled memory is injected into the prompt, in tokens. Leave empty to use ConvoMem's default (up to 800, or your agent's own setting). Lower it to keep the agent tighter on its node instructions.
     */
    convomem_memory_budget_tokens?: number;
}

/** Factory — sets `type` for you so you don't repeat the discriminator. */
export function convomem(input: Omit<Convomem, "type">): Convomem {
    return { type: "convomem", ...input };
}
