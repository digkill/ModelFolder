/**
 * Контракт «main unit»: трансформ + имена клипов по семантическим действиям.
 * rotation в градусах (Euler XYZ), position/scale в мировых единицах сцены.
 *
 * @typedef {Object} UnitMain
 * @property {[number, number, number]} position
 * @property {[number, number, number]} scale
 * @property {[number, number, number]} rotation
 * @property {Record<string, string|null>} animations
 */

export const UNIT_ACTIONS = Object.freeze([
  "attack",
  "def",
  "walk",
  "run",
  "death",
  "skill1",
  "skill2",
  "skill3",
  "ult",
  "idle",
  "jump",
  "wakeup",
  "fall",
  "block",
]);

/** @returns {UnitMain} */
export function defaultUnitMain() {
  return {
    position: [0, 0, 0],
    scale: [1, 1, 1],
    rotation: [0, 0, 0],
    animations: Object.fromEntries(UNIT_ACTIONS.map((a) => [a, null])),
  };
}

/**
 * @param {{ position: { set: Function }, scale: { set: Function }, rotation: { set: Function } }} root
 * @param {UnitMain} u
 */
export function applyUnitTransform(root, u) {
  const rad = Math.PI / 180;
  const [px, py, pz] = u.position;
  const [sx, sy, sz] = u.scale;
  const [rx, ry, rz] = u.rotation;
  root.position.set(px, py, pz);
  root.scale.set(sx, sy, sz);
  root.rotation.set(rx * rad, ry * rad, rz * rad);
}

/**
 * @param {UnitMain} u
 * @returns {string}
 */
export function serializeUnitMain(u) {
  return JSON.stringify(u, null, 2);
}
