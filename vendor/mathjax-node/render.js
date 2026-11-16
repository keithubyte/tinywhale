'use strict';
require('mathjax-full/js/util/entities/all.js');
const readline = require('readline');
const {mathjax} = require('mathjax-full/js/mathjax.js');
const {TeX} = require('mathjax-full/js/input/tex.js');
const {SVG} = require('mathjax-full/js/output/svg.js');
const {liteAdaptor} = require('mathjax-full/js/adaptors/liteAdaptor.js');
const {RegisterHTMLHandler} = require('mathjax-full/js/handlers/html.js');
const {AllPackages} = require('mathjax-full/js/input/tex/AllPackages.js');

const adaptor = liteAdaptor();
RegisterHTMLHandler(adaptor);
const tex = new TeX({packages: AllPackages});
const svg = new SVG({fontCache: 'local'});
const doc = mathjax.document('', {InputJax: tex, OutputJax: svg});

const rl = readline.createInterface({input: process.stdin});
rl.on('line', (line) => {
  try {
    const req = JSON.parse(line);
    const em = Number(req.em) || 16;
    const node = doc.convert(String(req.tex || ''), {
      display: !!req.display, em: em, ex: em * 0.5,
      containerWidth: 80 * em
    });
    process.stdout.write(JSON.stringify({ok: true, svg: adaptor.outerHTML(node)}) + '\n');
  } catch (err) {
    process.stdout.write(JSON.stringify({ok: false, error: String(err)}) + '\n');
  }
});
