/** Phaser-independent office layout constants safe to import from app routes. */
export const TILE_SIZE = 32
export const OFFICE_COLS = 20
export const OFFICE_ROWS = 25
export const GAP_COLS = 2
export const OFFICE_COUNT = 3
export const WORLD_COLS = OFFICE_COLS * OFFICE_COUNT + GAP_COLS * (OFFICE_COUNT - 1)
export const WORLD_ROWS = OFFICE_ROWS
export const MAP_COLS = WORLD_COLS
export const MAP_ROWS = WORLD_ROWS
