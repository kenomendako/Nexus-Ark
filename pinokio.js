module.exports = {
  title: "Nexus Ark",
  description: "AI Persona Interaction System with localized memory and emotional intelligence.",
  icon: "icon.png",
  menu: async (kernel) => {
    return [{
      html: '<i class="fa-solid fa-download"></i> Install',
      href: "install.js"
    }]
  }
}
