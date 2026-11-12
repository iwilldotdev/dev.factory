// Lets `node --test claude/hooks/test/` resolve the directory; loads every *.test.mjs here.
'use strict';
const fs = require('fs');
const path = require('path');
const { pathToFileURL } = require('url');

for (const name of fs.readdirSync(__dirname).filter((f) => f.endsWith('.test.mjs')).sort()) {
  import(pathToFileURL(path.join(__dirname, name)).href);
}
