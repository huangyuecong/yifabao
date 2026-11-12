import { describe, expect, it } from 'vitest'
import {
  addItem,
  createTimeline,
  findTrack,
  moveVisualItemToTrack,
  parseSubtitleText,
  rebaseTimelineOperationTrackIds,
  rebaseTimelineTrackIds,
  reorderItem,
  splitItem,
  timelineDuration,
} from './timelineState.ts'

function visual(id, duration) {
  return {
    id,
    project_asset_id: `asset-${id}`,
    label: id,
    duration_seconds: duration,
  }
}

describe('video editor timeline state', () => {
  it('creates the six stable editor tracks', () => {
    expect(createTimeline().tracks.map(track => track.kind)).toEqual([
      'video',
      'overlay',
      'subtitle',
      'voice',
      'music',
      'sfx',
    ])
  })

  it('keeps the visual track compact when clips are added and reordered', () => {
    let timeline = addItem(createTimeline(), 'video', visual('a', 2))
    timeline = addItem(timeline, 'video', visual('b', 3))
    timeline = addItem(timeline, 'video', visual('c', 4))
    timeline = reorderItem(timeline, 'video', 'c', 0)

    const items = findTrack(timeline, 'video')?.items || []
    expect(items.map(item => item.id)).toEqual(['c', 'a', 'b'])
    expect(items.map(item => item.start_seconds)).toEqual([0, 4, 6])
    expect(timelineDuration(timeline)).toBe(9)
  })

  it('moves an image from the main track to the overlay at its original time', () => {
    let timeline = addItem(createTimeline(), 'video', {
      ...visual('a', 2),
      metadata: { media_kind: 'image' },
    })
    timeline = addItem(timeline, 'video', {
      ...visual('b', 3),
      metadata: { media_kind: 'image' },
    })
    timeline = addItem(timeline, 'video', {
      ...visual('c', 4),
      metadata: { media_kind: 'image' },
    })

    const moved = moveVisualItemToTrack(timeline, 'b', 'overlay')

    expect(findTrack(moved, 'video')?.items.map(item => [item.id, item.start_seconds])).toEqual([
      ['a', 0],
      ['c', 2],
    ])
    expect(findTrack(moved, 'overlay')?.items.map(item => [item.id, item.start_seconds])).toEqual([
      ['b', 2],
    ])
    expect(findTrack(timeline, 'video')?.items.map(item => item.id)).toEqual(['a', 'b', 'c'])
  })

  it('returns an overlay image to its chronological main-track position', () => {
    let timeline = addItem(createTimeline(), 'video', {
      ...visual('a', 2),
      metadata: { media_kind: 'image' },
    })
    timeline = addItem(timeline, 'video', {
      ...visual('c', 4),
      metadata: { media_kind: 'image' },
    })
    timeline = addItem(timeline, 'overlay', {
      ...visual('b', 3),
      start_seconds: 2,
      metadata: { media_kind: 'image' },
    })

    const moved = moveVisualItemToTrack(timeline, 'b', 'video')

    expect(findTrack(moved, 'video')?.items.map(item => [item.id, item.start_seconds])).toEqual([
      ['a', 0],
      ['b', 2],
      ['c', 5],
    ])
    expect(findTrack(moved, 'overlay')?.items).toEqual([])
  })

  it('does not move videos into the image-only overlay track or cross a locked track', () => {
    let timeline = addItem(createTimeline(), 'video', {
      ...visual('video', 5),
      metadata: { media_kind: 'video' },
    })
    expect(moveVisualItemToTrack(timeline, 'video', 'overlay')).toBe(timeline)

    timeline = addItem(timeline, 'video', {
      ...visual('image', 3),
      metadata: { media_kind: 'image' },
    })
    const overlay = findTrack(timeline, 'overlay')
    if (overlay) {
      overlay.locked = true
    }
    expect(moveVisualItemToTrack(timeline, 'image', 'overlay')).toBe(timeline)
  })

  it('splits a clip at an absolute playhead while preserving total duration', () => {
    let timeline = addItem(createTimeline(), 'video', visual('a', 8))
    timeline = splitItem(timeline, 'a', 3, 'a-right')

    const items = findTrack(timeline, 'video')?.items || []
    expect(items.map(item => [item.id, item.start_seconds, item.duration_seconds])).toEqual([
      ['a', 0, 3],
      ['a-right', 3, 5],
    ])
    expect(items[1]?.trim_in_seconds).toBe(3)
    expect(timelineDuration(timeline)).toBe(8)
  })

  it('rebases a new local draft onto persisted track ids without losing edits', () => {
    const local = addItem(createTimeline(), 'video', visual('product', 4))
    const persisted = createTimeline()
    for (const track of persisted.tracks) {
      track.id = `${track.kind}-server-id`
    }
    const operations = [
      {
        type: 'add',
        track_id: 'track-video',
        track_kind: 'video',
        item: findTrack(local, 'video')?.items[0],
      },
      {
        type: 'update_track',
        track_id: 'track-music',
        changes: { muted: true },
      },
    ]

    const timeline = rebaseTimelineTrackIds(local, persisted)
    const rebasedOperations = rebaseTimelineOperationTrackIds(operations, local, persisted)

    expect(findTrack(timeline, 'video')?.id).toBe('video-server-id')
    expect(findTrack(timeline, 'video')?.items.map(item => item.id)).toEqual(['product'])
    expect(rebasedOperations.map(operation => operation.track_id)).toEqual([
      'video-server-id',
      'music-server-id',
    ])
  })

  it('parses SRT, VTT and plain text subtitle inputs', () => {
    const srt = parseSubtitleText('1\n00:00:01,000 --> 00:00:03,500\n雅酿酒业')
    const vtt = parseSubtitleText('WEBVTT\n\n00:00.000 --> 00:02.000\n品质之选', 'vtt')
    const text = parseSubtitleText('第一句\n第二句', 'txt')

    expect(srt).toEqual([{ start_seconds: 1, end_seconds: 3.5, text: '雅酿酒业' }])
    expect(vtt).toEqual([{ start_seconds: 0, end_seconds: 2, text: '品质之选' }])
    expect(text.map(cue => cue.start_seconds)).toEqual([0, 3])
  })
})
