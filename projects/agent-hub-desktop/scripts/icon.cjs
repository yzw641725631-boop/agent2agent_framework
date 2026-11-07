'use strict';
const fs=require('node:fs');
const path=require('node:path');
let bytes;
function icon(){if(!bytes)bytes=fs.readFileSync(path.resolve(__dirname,'..','assets','agent-hub.png'));return bytes;}
module.exports={icon};
