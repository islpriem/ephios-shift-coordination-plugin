// The local draft holds both roles: regular shifts and people sitting in.
// Replacing it never writes to the server; saving remains a separate operation.
export class DraftState {
    constructor(assignments, observers = []) { this.replace(assignments, observers); }
    replace(assignments, observers = []) {
        this.roles = new Map();
        for (const [person, shift] of assignments) this.roles.set(`${person}:${shift}`, "regular");
        for (const [person, shift] of observers) this.roles.set(`${person}:${shift}`, "observer");
    }
    has(person, shift) { return this.roles.has(`${person}:${shift}`); }
    role(person, shift) { return this.roles.get(`${person}:${shift}`); }
    toggle(person, shift, checked, observer = false) {
        const key = `${person}:${shift}`;
        if (checked) this.roles.set(key, observer ? "observer" : "regular");
        else this.roles.delete(key);
    }
    pairs(role) {
        return [...this.roles.entries()]
            .filter(([, value]) => value === role)
            .map(([key]) => key.split(":").map(Number));
    }
    assignments() { return this.pairs("regular"); }
    observers() { return this.pairs("observer"); }
    all() { return [...this.roles.keys()].map(key => key.split(":").map(Number)); }
    load(person, shifts) {
        const key = sid => shifts.get(sid).event_id ?? shifts.get(sid).date;
        const regular = this.assignments().filter(([uid]) => uid === person);
        const served = new Set(regular.map(([, sid]) => key(sid)));
        return regular.length + this.observers().filter(([uid, sid]) => uid === person && !served.has(key(sid))).length;
    }
}
