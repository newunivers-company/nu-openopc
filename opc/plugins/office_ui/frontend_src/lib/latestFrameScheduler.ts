export interface LatestFrameScheduler<T> {
  schedule(value: T): void
  dispose(): void
}

/** Coalesce rapid updates and apply only the newest value on the next frame. */
export function createLatestFrameScheduler<T>(
  apply: (value: T) => void,
  requestFrame: (callback: FrameRequestCallback) => number,
  cancelFrame: (handle: number) => void,
): LatestFrameScheduler<T> {
  let active = true
  let frame: number | null = null
  let pending: T | undefined
  let hasPending = false

  const flush = () => {
    frame = null
    if (!active || !hasPending) return

    const value = pending as T
    pending = undefined
    hasPending = false
    apply(value)
  }

  return {
    schedule(value) {
      if (!active) return
      pending = value
      hasPending = true
      if (frame === null) frame = requestFrame(flush)
    },
    dispose() {
      if (!active) return
      active = false
      pending = undefined
      hasPending = false
      if (frame !== null) cancelFrame(frame)
      frame = null
    },
  }
}
