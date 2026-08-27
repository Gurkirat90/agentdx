/** Join CSS Modules class names, dropping falsy values. CSS Modules resolve to `{ [key:
 * string]: string }`, and `noUncheckedIndexedAccess` (tsconfig) makes every such access
 * `string | undefined` at the type level even though the key always exists at runtime — this
 * is the one place that gets coalesced back to a definite `string`. */
export function cx(...classes: Array<string | undefined | false | null>): string {
  return classes.filter(Boolean).join(' ');
}
