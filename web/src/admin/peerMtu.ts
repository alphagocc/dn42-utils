import type { FieldDef } from "../shared/components/Modal";

const minMtu = 1280;
const maxMtu = 65535;

export function mtuField(value?: number | null): FieldDef {
  return {
    name: "mtu",
    label: `WireGuard MTU (${minMtu}–${maxMtu}, blank=auto)`,
    type: "number",
    value: value ?? "",
  };
}

export function parseMtu(value: string): number | null {
  if (!value) return null;
  const mtu = Number(value);
  if (!Number.isInteger(mtu) || mtu < minMtu || mtu > maxMtu) {
    throw new Error(`MTU must be an integer between ${minMtu} and ${maxMtu}.`);
  }
  return mtu;
}
