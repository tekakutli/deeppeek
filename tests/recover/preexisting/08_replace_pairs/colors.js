export const colors = {
  primary: "#ff0000",
  secondary: "#00ff00",
  accent: "#0000ff",
};

export function paint(el) {
  el.style.color = colors.primary;
  el.style.background = colors.secondary;
}
