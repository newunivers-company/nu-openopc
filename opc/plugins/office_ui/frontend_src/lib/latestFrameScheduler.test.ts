import assert from 'node:assert/strict'

import { createLatestFrameScheduler } from './latestFrameScheduler'

const callbacks = new Map<number, FrameRequestCallback>()
const cancelled: number[] = []
let nextHandle = 1
const applied: number[] = []

const scheduler = createLatestFrameScheduler(
  (value: number) => applied.push(value),
  (callback) => {
    const handle = nextHandle++
    callbacks.set(handle, callback)
    return handle
  },
  (handle) => {
    cancelled.push(handle)
    callbacks.delete(handle)
  },
)

scheduler.schedule(10)
scheduler.schedule(20)
assert.equal(callbacks.size, 1, 'updates in one frame should share one callback')
callbacks.get(1)?.(0)
callbacks.delete(1)
assert.deepEqual(applied, [20], 'the newest value should win')

scheduler.schedule(30)
assert.equal(callbacks.size, 1)
scheduler.dispose()
assert.deepEqual(cancelled, [2], 'dispose should cancel the pending frame')
assert.equal(callbacks.size, 0)

scheduler.schedule(40)
assert.equal(callbacks.size, 0, 'disposed schedulers should ignore later updates')
assert.deepEqual(applied, [20])

console.log('latestFrameScheduler tests passed')
