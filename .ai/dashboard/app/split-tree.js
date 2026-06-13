// Pure split-tree engine for the canvas window. No DOM, no globals beyond
// the single window.SplitTree export at the end — keeps it node-extractable.
function stEmpty() { return null; }
function stInsertFirst(tree, key) {
  if (tree !== null && tree !== undefined) throw new Error("insertFirst on non-empty tree");
  return { leaf: key };
}
function stSplitLeaf(tree, targetKey, newKey, dir) {
  var orient = (dir === "left" || dir === "right") ? "row" : "col";
  var newLeaf = { leaf: newKey };
  var replace = function(node) {
    if (node.leaf !== undefined) {
      if (node.leaf !== targetKey) return { leaf: node.leaf };
      var children = (dir === "left" || dir === "top")
        ? [newLeaf, { leaf: targetKey }]
        : [{ leaf: targetKey }, newLeaf];
      return { split: orient, ratios: [0.5, 0.5], children: children };
    }
    return { split: node.split, ratios: node.ratios.slice(),
             children: node.children.map(replace) };
  };
  return replace(tree);
}
function stNormalize(ratios) {
  var sum = ratios.reduce(function(a, b) { return a + b; }, 0) || 1;
  return ratios.map(function(r) { return r / sum; });
}
function stRemove(tree, key) {
  var rec = function(node) {
    if (node.leaf !== undefined) return node.leaf === key ? null : { leaf: node.leaf };
    var kept = [], keptRatios = [];
    node.children.forEach(function(c, i) {
      var r = rec(c);
      if (r !== null) { kept.push(r); keptRatios.push(node.ratios[i]); }
    });
    if (kept.length === 0) return null;
    if (kept.length === 1) return kept[0];
    return { split: node.split, ratios: stNormalize(keptRatios), children: kept };
  };
  return rec(tree);
}
function stKeys(tree) {
  if (tree === null || tree === undefined) return [];
  if (tree.leaf !== undefined) return [tree.leaf];
  var result = [];
  tree.children.forEach(function(c) {
    stKeys(c).forEach(function(k) { result.push(k); });
  });
  return result;
}
var ST_MIN_RATIO = 0.1;
function stResize(tree, path, delta) {
  var clone = function(node) {
    if (node.leaf !== undefined) return { leaf: node.leaf };
    return { split: node.split, ratios: node.ratios.slice(),
             children: node.children.map(clone) };
  };
  var root = clone(tree);
  var node = root;
  for (var i = 0; i < path.length; i++) { node = node.children[path[i]]; }
  // Guard: if node is falsy, a leaf, or lacks ratios, return the cloned tree unchanged
  if (!node || node.leaf !== undefined || !Array.isArray(node.ratios)) return root;
  // node is the addressed split — shift boundary between child[0] and child[1]
  var r0 = node.ratios[0], r1 = node.ratios[1];
  var newR0 = Math.min(Math.max(r0 + delta, ST_MIN_RATIO), r0 + r1 - ST_MIN_RATIO);
  node.ratios[0] = newR0;
  node.ratios[1] = (r0 + r1) - newR0;
  return root;
}
function stSerialize(tree) {
  if (tree === null || tree === undefined) return null;
  if (tree.leaf !== undefined) return { leaf: tree.leaf };
  return { split: tree.split, ratios: tree.ratios.slice(),
           children: tree.children.map(stSerialize) };
}
function stDeserialize(obj) {
  if (obj === null || obj === undefined) return null;
  if (obj.leaf !== undefined) {
    if (typeof obj.leaf !== "string") return null;
    return { leaf: obj.leaf };
  }
  if (obj.split === undefined) return null;
  if (obj.split !== "row" && obj.split !== "col") return null;
  if (!Array.isArray(obj.children) || !Array.isArray(obj.ratios)) return null;
  if (obj.children.length !== obj.ratios.length) return null;
  if (obj.children.length === 0) return null;
  if (obj.ratios.some(function (r) { return typeof r !== "number" || r <= 0; })) return null;
  var kids = [];
  for (var i = 0; i < obj.children.length; i++) {
    var k = stDeserialize(obj.children[i]);
    if (k === null) return null;
    kids.push(k);
  }
  return { split: obj.split, ratios: obj.ratios.slice(), children: kids };
}
// Pure layout geometry. Walks the split tree, assigning each leaf an
// absolute rect within the (w x h) viewport. A `row` split divides width
// across children by their normalized ratios (x offsets accumulate); a
// `col` split divides height (y offsets accumulate). Returns a FLAT array
// of {key, x, y, w, h} in in-order. NO gutter subtraction — gutters are
// drawn as overlays on top of these rects, not carved out of them.
//
// Multiplication is exact (no rounding) so a 0.5/0.5 split of 100 yields
// exactly 50/50; the DOM renderer can absolutely-position from these.
function stComputeRects(tree, w, h) {
  var out = [];
  var walk = function (node, x, y, ww, hh) {
    if (node === null || node === undefined) return;
    if (node.leaf !== undefined) {
      out.push({ key: node.leaf, x: x, y: y, w: ww, h: hh });
      return;
    }
    var ratios = stNormalize(node.ratios);
    var isRow = node.split === "row";
    var offset = 0; // along the split axis
    for (var i = 0; i < node.children.length; i++) {
      var frac = ratios[i];
      if (isRow) {
        var cw = ww * frac;
        walk(node.children[i], x + offset, y, cw, hh);
        offset += cw;
      } else {
        var ch = hh * frac;
        walk(node.children[i], x, y + offset, ww, ch);
        offset += ch;
      }
    }
  };
  walk(tree, 0, 0, w, h);
  return out;
}
window.SplitTree = {
  empty: stEmpty, insertFirst: stInsertFirst, splitLeaf: stSplitLeaf,
  remove: stRemove, keys: stKeys, resize: stResize,
  serialize: stSerialize, deserialize: stDeserialize,
  computeRects: stComputeRects
};
