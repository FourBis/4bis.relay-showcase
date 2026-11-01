// Fechas de calendario compartidas por Informe y Métricas.
export function localToday(now = new Date()) {
  return `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}-${String(now.getDate()).padStart(2, "0")}`;
}

export function dateRange(from, to) {
  if (!from || !to) throw new Error("Completa las fechas desde y hasta.");
  const valid = (value) => /^[0-9]{4}-[0-9]{2}-[0-9]{2}$/.test(value)
    && value.slice(0, 4) !== "0000"
    && Number.isFinite(Date.parse(`${value}T00:00:00Z`))
    && new Date(`${value}T00:00:00Z`).toISOString().slice(0, 10) === value;
  if (!valid(from) || !valid(to)) throw new Error("Introduce fechas válidas.");
  if (from > to) throw new Error("La fecha hasta no puede ser anterior a desde.");
  const days = (Date.parse(`${to}T00:00:00Z`) - Date.parse(`${from}T00:00:00Z`)) / 86400000 + 1;
  if (days > 366) throw new Error("El rango máximo es de 366 días.");
  return { from, to, days, tz: Intl.DateTimeFormat().resolvedOptions().timeZone };
}
