// ---------------------------------------------------------------------
// v18: graph undo/redo engine — snapshot-based history for the editor.
// The editor commits a snapshot after every completed mutation
// (add/delete/connect/drag/config change); undo/redo walk the stack and
// return plain {nodes, edges} graphs that the page re-renders.
// ---------------------------------------------------------------------
import { computed, nextTick, ref } from 'vue'

export interface GraphSnapshot {
  nodes: any[]
  edges: any[]
}

export function createGraphHistory(limit = 80) {
  const stack = ref<string[]>([])
  const pointer = ref(-1)
  // While an undo/redo snapshot is being applied, commits are suspended so
  // the application itself can never pollute the stack.
  let suspended = false

  function snap(g: GraphSnapshot) {
    return JSON.stringify(g)
  }

  /** Fresh history (initial load, version restore, import) */
  function reset(g: GraphSnapshot) {
    stack.value = [snap(g)]
    pointer.value = 0
  }

  /** Commit after a completed user mutation. No-ops and undo/redo-driven changes are ignored. */
  function commit(g: GraphSnapshot) {
    if (suspended) return
    const s = snap(g)
    if (stack.value[pointer.value] === s) return // identical to top — nothing changed
    const trimmed = stack.value.slice(0, pointer.value + 1) // drop redo branch
    trimmed.push(s)
    while (trimmed.length > limit) trimmed.shift()
    stack.value = trimmed
    pointer.value = trimmed.length - 1
  }

  const canUndo = computed(() => pointer.value > 0)
  const canRedo = computed(() => pointer.value < stack.value.length - 1)

  function step(delta: number): GraphSnapshot | null {
    const next = pointer.value + delta
    if (next < 0 || next > stack.value.length - 1) return null
    pointer.value = next
    suspended = true
    nextTick(() => {
      suspended = false
    })
    return JSON.parse(stack.value[pointer.value])
  }

  function undo(): GraphSnapshot | null {
    return step(-1)
  }

  function redo(): GraphSnapshot | null {
    return step(1)
  }

  return { reset, commit, undo, redo, canUndo, canRedo }
}
