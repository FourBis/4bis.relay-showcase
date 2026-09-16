// Config de Tailwind v3 para compilar el CSS del admin (ver build-css.ps1).
// content: el scanner lee clases en el HTML y en los templates de los .js
// (los innerHTML dinámicos usan literales completos — verificado: las únicas
// interpolaciones son budget-seg-*, clases propias no-Tailwind).
module.exports = {
  content: ["./index.html", "./static/**/*.js"],
  theme: { extend: {} },
  plugins: [],
};
