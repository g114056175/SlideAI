// Deduplicate image work and promote selected pages ahead of background work.
export function createImageQueue(concurrency = 3) {
  let active = 0
  const pending = []
  const tasks = new Map()
  const pump = () => {
    while (active < concurrency && pending.length) {
      const task = pending.shift()
      active++
      Promise.resolve().then(task.run).then(task.resolve, task.reject).finally(() => {
        active--
        tasks.delete(task.key)
        pump()
      })
    }
  }
  return (key, run, priority = false) => {
    const existing = tasks.get(key)
    if (existing) {
      const index = pending.indexOf(existing)
      if (priority && index > 0) {
        pending.splice(index, 1)
        pending.unshift(existing)
      }
      return existing.promise
    }
    let resolve, reject
    const promise = new Promise((yes, no) => { resolve = yes; reject = no })
    const task = { key, run, promise, resolve, reject }
    tasks.set(key, task)
    if (priority) pending.unshift(task)
    else pending.push(task)
    pump()
    return promise
  }
}
