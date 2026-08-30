// Global command-palette state (v14) — shared by the palette component, the
// sidebar trigger button and any page that wants to pop it open.
export function usePalette() {
  const open = useState<boolean>('py8n-palette-open', () => false)

  function openPalette() {
    open.value = true
  }

  function closePalette() {
    open.value = false
  }

  function togglePalette() {
    open.value = !open.value
  }

  return { open, openPalette, closePalette, togglePalette }
}
