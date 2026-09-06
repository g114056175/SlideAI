import test from 'node:test'
import assert from 'node:assert/strict'
import { createImageQueue } from '../src/utils/image-queue.js'
const tick = () => new Promise(resolve => setImmediate(resolve))

test('limits concurrency and promotes a clicked queued page without a duplicate fetch', async () => {
  const enqueue = createImageQueue(1)
  const started = []
  let finishFirst
  const first = enqueue('first', () => new Promise(resolve => { started.push('first'); finishFirst = resolve }))
  const second = enqueue('second', () => { started.push('second') })
  const third = enqueue('third', () => { started.push('third') })
  assert.equal(enqueue('third', () => assert.fail('duplicate'), true), third)
  await tick()
  assert.deepEqual(started, ['first'])
  finishFirst()
  await Promise.all([first, second, third])
  assert.deepEqual(started, ['first', 'third', 'second'])
})

test('initial images settle before background work is submitted', async () => {
  const enqueue = createImageQueue(3)
  let finish
  const first = enqueue('first-large', () => new Promise(resolve => { finish = resolve }))
  const small = enqueue('small', () => {})
  let shown = false, background = false
  const opening = Promise.all([first, small]).then(async () => {
    shown = true
    await enqueue('background-large', () => { background = true })
  })
  await tick()
  assert.equal(shown, false)
  assert.equal(background, false)
  finish()
  await opening
  assert.equal(background, true)
})

test('failure frees a slot and stale sessions can skip queued requests', async () => {
  const enqueue = createImageQueue(1)
  let session = 1, staleFetched = false
  const failed = enqueue('failure', () => { throw new Error('network') })
  const old = enqueue('page:session1', () => { if (session === 1) staleFetched = true })
  session = 2
  let freshFetched = false
  const fresh = enqueue('page:session2', () => { freshFetched = true })
  await assert.rejects(failed, /network/)
  await Promise.all([old, fresh])
  assert.equal(staleFetched, false)
  assert.equal(freshFetched, true)
})
