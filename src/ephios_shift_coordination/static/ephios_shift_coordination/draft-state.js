// The local draft can also accept a complete proposal after explicit UI acceptance.
// Replacing it never writes to the server; saving remains a separate operation.
export class DraftState {
    constructor(assignments) { this.replace(assignments); }
    replace(assignments) { this.selected = new Set(assignments.map(pair => pair.join(":"))); }
    has(person, shift) { return this.selected.has(`${person}:${shift}`); }
    toggle(person, shift, checked) {
        const key = `${person}:${shift}`;
        if (checked) this.selected.add(key);
        else this.selected.delete(key);
    }
    assignments() { return [...this.selected].map(key => key.split(":").map(Number)); }
}
