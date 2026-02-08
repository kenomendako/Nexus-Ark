const os = require('os')
const fs = require('fs')
const path = require('path')

module.exports = {
  title: "Nexus Ark",
  description: "AI Persona Interaction System with localized memory and emotional intelligence.",
  icon: "icon.png",
  menu: async (kernel) => {
    let installed = await kernel.exists(__dirname, "venv") || await kernel.exists(__dirname, ".venv")
    if (process.platform === 'win32') {
      installed = installed || await kernel.exists(__dirname, "uv")
    }

    if (installed) {
      return [{
        html: '<i class="fa-solid fa-rocket"></i> Start Application',
        href: "start.js"
      }, {
        html: '<i class="fa-solid fa-rotate"></i> Update',
        href: "update.js"
      }, {
        html: '<i class="fa-solid fa-folder"></i> Open Folder',
        href: "explorer:" + __dirname
      }]
    } else {
      return [{
        html: '<i class="fa-solid fa-download"></i> Install',
        href: "install.js"
      }]
    }
  }
}
