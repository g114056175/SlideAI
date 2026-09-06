import { computed, onBeforeUnmount, onMounted, ref } from 'vue'

// Menus across the workspace share one active owner.
const activeMenu = ref(null)

export function useExclusiveMenu() {
  const owner = Symbol('menu')
  const root = ref(null)
  const open = computed(() => activeMenu.value === owner)
  const close = () => { if (open.value) activeMenu.value = null }
  const toggle = () => { activeMenu.value = open.value ? null : owner }
  const onPointerDown = event => {
    if (open.value && !root.value?.contains(event.target)) close()
  }
  const onKeyDown = event => {
    if (open.value && event.key === 'Escape') {
      close()
      root.value?.querySelector('button')?.focus()
    }
  }
  onMounted(() => {
    document.addEventListener('pointerdown', onPointerDown)
    document.addEventListener('keydown', onKeyDown)
  })
  onBeforeUnmount(() => {
    close()
    document.removeEventListener('pointerdown', onPointerDown)
    document.removeEventListener('keydown', onKeyDown)
  })
  return { root, open, toggle, close }
}
