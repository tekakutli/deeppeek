export const colors = {
  primary: "#88c0d0",
  secondary: "#a3be8c",
  accent: "#b48ead",
};

export function paint(el) {
  el.style.color = colors.primary;
  el.style.background = colors.secondary;
}
