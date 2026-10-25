use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::borrow::Cow;
use std::io::{self, BufRead, Write};

const DIRS: [[i32; 2]; 6] = [[0, -1], [1, -1], [1, 0], [0, 1], [-1, 1], [-1, 0]];
const WIN: i32 = 1_000_000;

#[derive(Clone, Copy, Debug, Serialize, Deserialize, PartialEq, Eq, PartialOrd, Ord)]
#[serde(rename_all = "lowercase")]
enum Kind {
    King,
    Guard,
    Scout,
    Lancer,
    Frog,
    Queen,
    Revenant,
    Ghost,
    Ram,
    Warper,
    #[serde(alias = "ranger")]
    Cleaver,
    Pawn, Crab, Flanker, Strider, Dancer, Hound, Triton, Dragoon, Courser,
    Pike, Rook, Bishop, Chariot, Triskelion, Knight, Locust, Cricket,
    /// V10 royal: a Lancer's 1--3 forward ray plus one step on every ray.
    #[serde(rename = "crowned_lancer")]
    CrownedLancer,
    /// V10 Fey infantry: Scout fan at one step and a two-step forward lane.
    #[serde(rename = "charmed_strider")]
    CharmedStrider,
    /// V12 Fey controller: Scout movement, no captures, roots adjacent enemies.
    Enchanter,
    /// V12 Fey exchanger: range-three, six-ray enemy-only swaps, no captures.
    Exchanger,
    /// V14 Fey relocator: swaps with any friendly piece anywhere (no line,
    /// range or obstruction); no ordinary move, never captures, inert facing.
    Universal,
}
#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
struct Piece {
    q: i32,
    r: i32,
    kind: Kind,
    side: u8,
    facing: u8,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct State {
    rules_version: String,
    radius: i32,
    board: Vec<Piece>,
    turn: u8,
    reserves: [u32; 2],
    ply: u64,
    history: Vec<String>,
}
#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
struct Action {
    kind: Cow<'static, str>,
    from: Option<[i32; 2]>,
    to: Option<[i32; 2]>,
    facing: Option<u8>,
}
#[derive(Clone, Debug, Serialize)]
struct Outcome {
    status: &'static str,
    winner: Option<u8>,
    reason: &'static str,
}
impl Outcome {
    fn ongoing() -> Self {
        Self {
            status: "ongoing",
            winner: None,
            reason: "",
        }
    }
}
fn distance(q: i32, r: i32) -> i32 {
    q.abs().max(r.abs()).max((q + r).abs())
}
fn inside(s: &State, p: [i32; 2]) -> bool {
    distance(p[0], p[1]) <= s.radius
}
fn at(s: &State, p: [i32; 2]) -> Option<usize> {
    s.board.iter().position(|x| [x.q, x.r] == p)
}
struct Occupancy([i16; 121]);
impl Occupancy {
    fn new(s: &State) -> Self {
        let mut cells = [-1; 121];
        for (i, p) in s.board.iter().enumerate() {
            cells[((p.q + 5) * 11 + p.r + 5) as usize] = i as i16;
        }
        Self(cells)
    }
    #[inline]
    fn at(&self, p: [i32; 2]) -> Option<usize> {
        if !(-5..=5).contains(&p[0]) || !(-5..=5).contains(&p[1]) { return None; }
        let i = self.0[((p[0] + 5) * 11 + p[1] + 5) as usize];
        if i < 0 { None } else { Some(i as usize) }
    }
}
fn directional(k: Kind) -> bool {
    matches!(k, Kind::Scout | Kind::Revenant | Kind::Lancer | Kind::Pawn | Kind::Crab
        | Kind::Flanker | Kind::Strider | Kind::Dancer | Kind::Hound | Kind::Triton
        | Kind::Dragoon | Kind::Pike | Kind::Rook | Kind::Bishop | Kind::Chariot
        | Kind::Triskelion | Kind::Knight | Kind::Locust | Kind::CrownedLancer
        | Kind::CharmedStrider | Kind::Enchanter)
}
#[inline]
fn halo_queen(s: &State, k: Kind) -> bool {
    k == Kind::Queen && matches!(s.rules_version.as_str(), "hexwar-lab-v11" | "hexwar-lab-v12" | "hexwar-lab-v12-fan" | "hexwar-lab-v13" | "hexwar-lab-v14")
}
#[inline]
fn royal(k: Kind) -> bool { matches!(k, Kind::King | Kind::CrownedLancer) }
fn ray_allowed(k: Kind, facing: u8, dir: usize) -> bool {
    let relative = (dir + 6 - facing as usize) % 6;
    match k {
        Kind::Pawn | Kind::Strider | Kind::Lancer | Kind::Pike => relative == 0,
        Kind::Scout | Kind::Revenant | Kind::Hound | Kind::Dragoon | Kind::Chariot | Kind::Enchanter
        | Kind::CharmedStrider => matches!(relative, 0 | 1 | 5),
        Kind::Crab | Kind::Triton | Kind::Triskelion => matches!(relative, 1 | 3 | 5),
        Kind::Flanker | Kind::Dancer => matches!(relative, 1 | 5),
        Kind::Rook => matches!(relative, 0 | 3),
        Kind::Bishop => matches!(relative, 1 | 2 | 4 | 5),
        _ => true,
    }
}
fn ray_limit(k: Kind, radius: i32, facing: u8, dir: usize) -> i32 {
    let relative = (dir + 6 - facing as usize) % 6;
    match k {
        Kind::CrownedLancer => if relative == 0 { 3 } else { 1 },
        Kind::CharmedStrider => if relative == 0 { 2 } else { 1 },
        _ => ray_range(k, radius),
    }
}
fn state_ray_limit(s: &State, k: Kind, facing: u8, dir: usize) -> i32 {
    if halo_queen(s, k) {
        let relative = (dir + 6 - facing as usize) % 6;
        if relative == 0 { s.radius * 2 } else { 1 }
    } else {
        ray_limit(k, s.radius, facing, dir)
    }
}
fn ray_range(k: Kind, radius: i32) -> i32 {
    match k {
        Kind::Queen | Kind::Ram | Kind::Warper | Kind::Universal | Kind::Pike | Kind::Rook
        | Kind::Bishop | Kind::Chariot | Kind::Triskelion => radius * 2,
        Kind::Lancer | Kind::Dragoon | Kind::Courser | Kind::Exchanger => 3,
        Kind::Cleaver | Kind::Strider | Kind::Dancer | Kind::Hound | Kind::Triton => 2,
        _ => 1,
    }
}
fn jump_range(k: Kind) -> Option<i32> {
    match k { Kind::Frog | Kind::Knight => Some(2), Kind::Locust | Kind::Cricket => Some(3), _ => None }
}
fn jump_allowed(k: Kind, facing: u8, mut q: i32, mut r: i32) -> bool {
    if !matches!(k, Kind::Knight | Kind::Locust) { return true; }
    // Rotate the offset back into facing zero. These exact ring coordinates
    // are the original ±75-degree arc, not three straight forward rays.
    for _ in 0..facing { (q, r) = (q + r, -q); }
    match k {
        Kind::Knight => matches!((q,r), (0,-2)|(1,-2)|(2,-2)|(-1,-1)|(-2,0)),
        Kind::Locust => matches!((q,r), (0,-3)|(1,-3)|(2,-3)|(3,-3)|(-1,-2)|(-2,-1)|(-3,0)),
        _ => unreachable!(),
    }
}
fn canon(s: &mut State) {
    s.board.sort_by_key(|p| (p.q, p.r));
}
fn key(s: &State) -> String {
    // Explicit stable encoding, independent of hash implementation or board input order.
    let mut b = s.board.clone();
    b.sort_by_key(|p| (p.q, p.r));
    if timed(s) {
        serde_json::to_string(&(&s.rules_version, s.radius, s.turn, s.reserves, s.ply, b)).unwrap()
    } else {
        serde_json::to_string(&(s.radius, s.turn, s.reserves, b)).unwrap()
    }
}
fn timed(s: &State) -> bool {
    matches!(s.rules_version.as_str(), "hexwar-lab-v2" | "hexwar-lab-v3" | "hexwar-lab-v4" | "hexwar-lab-v5" | "hexwar-lab-v6" | "hexwar-lab-v7" | "hexwar-lab-v8" | "hexwar-lab-v9" | "hexwar-lab-v10" | "hexwar-lab-v11" | "hexwar-lab-v12" | "hexwar-lab-v12-fan" | "hexwar-lab-v13" | "hexwar-lab-v14")
}
fn deadline(s: &State) -> u64 {
    if matches!(s.rules_version.as_str(), "hexwar-lab-v6" | "hexwar-lab-v7") { 70 }
    else if matches!(s.rules_version.as_str(), "hexwar-lab-v8" | "hexwar-lab-v9" | "hexwar-lab-v10" | "hexwar-lab-v11" | "hexwar-lab-v12" | "hexwar-lab-v12-fan" | "hexwar-lab-v13" | "hexwar-lab-v14") { 84 }
    else { 100 }
}
fn body_counts(s: &State) -> [i64; 2] {
    let mut counts = [0, 0];
    for p in &s.board { counts[p.side as usize] += 1; }
    counts
}
fn army_distances(s: &State) -> [i64; 2] {
    let mut totals = [0, 0];
    for p in &s.board { totals[p.side as usize] += distance(p.q, p.r) as i64; }
    totals
}
fn validate(s: &mut State) -> Result<(), String> {
    if s.rules_version != "hexwar-lab-v1" && !timed(s) {
        return Err("unsupported rules_version".into());
    }
    if !(2..=5).contains(&s.radius) || s.turn > 1 {
        return Err("invalid radius or turn".into());
    }
    // Leave room for every supported search horizon; reject instead of wrapping.
    if s.ply > (u32::MAX - 12) as u64 || (timed(s) && s.ply > deadline(s)) {
        return Err("ply overflow".into());
    }
    let mut kings = [0, 0];
    for p in &s.board {
        if p.kind == Kind::Cleaver && !matches!(s.rules_version.as_str(), "hexwar-lab-v4" | "hexwar-lab-v5" | "hexwar-lab-v6" | "hexwar-lab-v7" | "hexwar-lab-v8" | "hexwar-lab-v9" | "hexwar-lab-v10" | "hexwar-lab-v11" | "hexwar-lab-v12" | "hexwar-lab-v12-fan" | "hexwar-lab-v13" | "hexwar-lab-v14") {
            return Err("Cleaver requires hexwar-lab-v4".into());
        }
        if matches!(p.kind, Kind::CrownedLancer | Kind::CharmedStrider) && !matches!(s.rules_version.as_str(), "hexwar-lab-v10" | "hexwar-lab-v11" | "hexwar-lab-v12" | "hexwar-lab-v12-fan" | "hexwar-lab-v13" | "hexwar-lab-v14") {
            return Err("Crowned Lancer and Charmed Strider require hexwar-lab-v10".into());
        }
        if matches!(p.kind, Kind::Enchanter | Kind::Exchanger) && !matches!(s.rules_version.as_str(), "hexwar-lab-v12" | "hexwar-lab-v12-fan" | "hexwar-lab-v13" | "hexwar-lab-v14") {
            return Err("Enchanter and Exchanger require hexwar-lab-v12".into());
        }
        if p.kind == Kind::Universal && s.rules_version != "hexwar-lab-v14" {
            return Err("Universal requires hexwar-lab-v14".into());
        }
        if p.kind as usize > Kind::Cleaver as usize && !matches!(s.rules_version.as_str(), "hexwar-lab-v5" | "hexwar-lab-v6" | "hexwar-lab-v7" | "hexwar-lab-v8" | "hexwar-lab-v9" | "hexwar-lab-v10" | "hexwar-lab-v11" | "hexwar-lab-v12" | "hexwar-lab-v12-fan" | "hexwar-lab-v13" | "hexwar-lab-v14") {
            return Err("restored catalogue pieces require hexwar-lab-v5".into());
        }
        if !(-5..=5).contains(&p.q)
            || !(-5..=5).contains(&p.r)
            || !inside(s, [p.q, p.r])
            || p.side > 1
            || p.facing > 5
        {
            return Err("invalid piece coordinates, side or facing".into());
        }
        if royal(p.kind) {
            kings[p.side as usize] += 1;
        }
    }
    if kings.iter().any(|&n| n > 1) {
        return Err("multiple kings for one side".into());
    }
    for side in 0..2 {
        if s.board.iter().filter(|p| p.side == side && p.kind == Kind::Universal).count() > 1 {
            return Err("at most one Universal per side".into());
        }
    }
    if s.reserves.iter().any(|&n| n > 91) {
        return Err("invalid reserve count".into());
    }
    for side in 0..2 {
        let alive = s
            .board
            .iter()
            .filter(|p| p.side == side && p.kind == Kind::Revenant)
            .count();
        if alive + s.reserves[side as usize] as usize > 91 {
            return Err("more than 91 total Revenants for one side".into());
        }
    }
    canon(s);
    if s.board
        .windows(2)
        .any(|p| p[0].q == p[1].q && p[0].r == p[1].r)
    {
        return Err("overlapping pieces".into());
    }
    Ok(())
}
fn action(kind: &str, from: Option<[i32; 2]>, to: Option<[i32; 2]>, facing: Option<u8>) -> Action {
    Action {
        kind: match kind {
            "move" => Cow::Borrowed("move"),
            "rotate" => Cow::Borrowed("rotate"),
            "push" => Cow::Borrowed("push"),
            "swap" => Cow::Borrowed("swap"),
            "rebirth" => Cow::Borrowed("rebirth"),
            "pass" => Cow::Borrowed("pass"),
            other => Cow::Owned(other.to_owned()),
        },
        from,
        to,
        facing,
    }
}
/// V13 pass rule: a side with no other legal action passes (the clock
/// advances). Earlier versions keep the no-legal-action stalemate draw.
fn passes(s: &State) -> bool {
    matches!(s.rules_version.as_str(), "hexwar-lab-v13" | "hexwar-lab-v14")
}
fn moves(s: &State) -> Vec<Action> {
    let mut out = moves_for_activity(s, true);
    if out.is_empty() && passes(s) {
        out.push(action("pass", None, None, None));
    }
    out
}
fn moves_for_activity(s: &State, include_stationary: bool) -> Vec<Action> {
    let mut out = Vec::new();
    for_each_move(s, include_stationary, s.turn, |a| out.push(a));
    out
}
fn rooted(s: &State, p: &Piece) -> bool {
    match s.rules_version.as_str() {
        "hexwar-lab-v12" => s.board.iter().any(|other| {
            other.side != p.side && other.kind == Kind::Enchanter
                && distance(other.q - p.q, other.r - p.r) == 1
        }),
        // V12-fan: only the Enchanter's forward Scout fan (relative 0, +1, -1)
        // roots; side- and rear-adjacent enemies act normally.
        "hexwar-lab-v12-fan" | "hexwar-lab-v13" | "hexwar-lab-v14" => s.board.iter().any(|other| {
            other.side != p.side && other.kind == Kind::Enchanter
                && DIRS.iter().enumerate().any(|(d, v)| {
                    other.q + v[0] == p.q && other.r + v[1] == p.r
                        && ray_allowed(Kind::Enchanter, other.facing, d)
                })
        }),
        _ => false,
    }
}
fn rooted_count(s: &State, side: u8) -> usize {
    s.board.iter().filter(|p| p.side == side && rooted(s, p)).count()
}
fn for_each_move(s: &State, include_stationary: bool, side: u8, mut emit: impl FnMut(Action)) {
    let occupancy = Occupancy::new(s);
    for p in s.board.iter().filter(|p| p.side == side) {
        if rooted(s, p) {
            continue;
        }
        let from = [p.q, p.r];
        if p.kind == Kind::Universal {
            // V14: swap with any friendly piece anywhere; the arriving friend
            // chooses its facing, the Universal keeps its own. Like rotations
            // these are repositionings, so tactical mobility (stationary
            // excluded) does not count ~60 swaps as activity.
            if include_stationary {
                for other in s.board.iter().filter(|o| o.side == side && (o.q, o.r) != (p.q, p.r)) {
                    for facing in 0..6 {
                        emit(action("swap", Some(from), Some([other.q, other.r]), Some(facing)));
                    }
                }
            }
            continue;
        }
        if include_stationary && (directional(p.kind) || halo_queen(s, p.kind)) {
            for f in 0..6 {
                if f != p.facing {
                    emit(action("rotate", Some(from), None, Some(f)));
                }
            }
        }
        if let Some(range) = jump_range(p.kind) {
            for q in -range..=range {
                for r in -range..=range {
                    if distance(q, r) != range || !jump_allowed(p.kind, p.facing, q, r) {
                        continue;
                    }
                    let to = [p.q + q, p.r + r];
                    if inside(s, to) && occupancy.at(to).map_or(true, |i| s.board[i].side != side) {
                        emit(action("move", Some(from), Some(to), None));
                    }
                }
            }
            continue;
        }
        for (d, v) in DIRS.iter().enumerate() {
            if !ray_allowed(p.kind, p.facing, d) {
                continue;
            }
            let range = state_ray_limit(s, p.kind, p.facing, d);
            for n in 1..=range {
                let to = [p.q + n * v[0], p.r + n * v[1]];
                if !inside(s, to) {
                    break;
                }
                if let Some(i) = occupancy.at(to) {
                    let other = &s.board[i];
                    if matches!(p.kind, Kind::Warper | Kind::Exchanger) {
                        if (p.kind == Kind::Warper && other.side == side)
                            || (p.kind == Kind::Exchanger && other.side != side)
                        {
                            if matches!(s.rules_version.as_str(), "hexwar-lab-v9" | "hexwar-lab-v10" | "hexwar-lab-v11" | "hexwar-lab-v12" | "hexwar-lab-v12-fan" | "hexwar-lab-v13" | "hexwar-lab-v14") {
                                for facing in 0..6 {
                                    emit(action("swap", Some(from), Some(to), Some(facing)));
                                }
                            } else {
                                emit(action("swap", Some(from), Some(to), None));
                            }
                        }
                    } else if (other.side != side && p.kind != Kind::Enchanter)
                        || (p.kind == Kind::Ram && matches!(s.rules_version.as_str(), "hexwar-lab-v3" | "hexwar-lab-v4" | "hexwar-lab-v5" | "hexwar-lab-v6" | "hexwar-lab-v7" | "hexwar-lab-v8" | "hexwar-lab-v9" | "hexwar-lab-v10" | "hexwar-lab-v11" | "hexwar-lab-v12" | "hexwar-lab-v12-fan" | "hexwar-lab-v13" | "hexwar-lab-v14"))
                    {
                        if p.kind == Kind::Ram {
                            let beyond = [to[0] + v[0], to[1] + v[1]];
                            if inside(s, beyond) && occupancy.at(beyond).is_none() {
                                emit(action("push", Some(from), Some(to), None));
                            }
                        } else {
                            emit(action("move", Some(from), Some(to), None));
                        }
                    }
                    break;
                } else if !matches!(p.kind, Kind::Warper | Kind::Exchanger) {
                    emit(action("move", Some(from), Some(to), None));
                }
            }
        }
    }
    if include_stationary && s.reserves[side as usize] > 0 {
        if let Some(k) = s
            .board
            .iter()
        .find(|p| p.side == side && royal(p.kind))
        {
            for (d, v) in DIRS.iter().enumerate() {
                let to = [k.q + v[0], k.r + v[1]];
                if inside(s, to) && occupancy.at(to).is_none() {
                    emit(action("rebirth", None, Some(to), Some(((d + 3) % 6) as u8)));
                }
            }
        }
    }
}
fn terminal(s: &State) -> Outcome {
    let k0 = s.board.iter().any(|p| royal(p.kind) && p.side == 0);
    let k1 = s.board.iter().any(|p| royal(p.kind) && p.side == 1);
    match (k0, k1) {
        (false, false) => {
            return Outcome {
                status: "draw",
                winner: None,
                reason: "both_kings_lost",
            }
        }
        (false, true) => {
            return Outcome {
                status: "win",
                winner: Some(1),
                reason: "king_lost",
            }
        }
        (true, false) => {
            return Outcome {
                status: "win",
                winner: Some(0),
                reason: "king_lost",
            }
        }
        _ => {}
    }
    if timed(s) {
        if s.ply == deadline(s) {
            let mut d = [0, 0];
            for p in s.board.iter().filter(|p| royal(p.kind)) {
                d[p.side as usize] = distance(p.q, p.r);
            }
            return if d[0] == d[1] {
                let counts = body_counts(s);
                if matches!(s.rules_version.as_str(), "hexwar-lab-v6" | "hexwar-lab-v7" | "hexwar-lab-v8" | "hexwar-lab-v9" | "hexwar-lab-v10" | "hexwar-lab-v11" | "hexwar-lab-v12" | "hexwar-lab-v12-fan" | "hexwar-lab-v13" | "hexwar-lab-v14") && counts[0] != counts[1] {
                    return Outcome {
                        status: "win",
                        winner: Some(if counts[0] > counts[1] { 0 } else { 1 }),
                        reason: "deadline_piece_count",
                    };
                }
                if matches!(s.rules_version.as_str(), "hexwar-lab-v7" | "hexwar-lab-v8" | "hexwar-lab-v9" | "hexwar-lab-v10" | "hexwar-lab-v11" | "hexwar-lab-v12" | "hexwar-lab-v12-fan" | "hexwar-lab-v13" | "hexwar-lab-v14") {
                    let totals = army_distances(s);
                    if totals[0] != totals[1] {
                        return Outcome {
                            status: "win",
                            winner: Some(if totals[0] < totals[1] { 0 } else { 1 }),
                            reason: "deadline_army_proximity",
                        };
                    }
                }
                Outcome {
                    status: "draw",
                    winner: None,
                    reason: "deadline_equal",
                }
            } else {
                Outcome {
                    status: "win",
                    winner: Some(if d[0] < d[1] { 0 } else { 1 }),
                    reason: "deadline_proximity",
                }
            };
        }
        return Outcome::ongoing();
    }
    let k = key(s);
    if s.history.iter().filter(|x| **x == k).take(2).count() >= 2 {
        return Outcome {
            status: "draw",
            winner: None,
            reason: "threefold_repetition",
        };
    }
    Outcome::ongoing()
}
fn outcome(s: &State) -> Outcome {
    let t = terminal(s);
    if t.status != "ongoing" {
        t
    } else if moves(s).is_empty() {
        Outcome {
            status: "draw",
            winner: None,
            reason: "stalemate",
        }
    } else {
        t
    }
}
fn apply_unchecked(s: &State, a: &Action) -> State {
    apply_internal(s, a, true)
}
fn apply_search(s: &State, a: &Action) -> State {
    // Timed rules depend on the clock, never repetition history. Keep replay
    // bookkeeping at the public boundary; v1 must retain exact repetition.
    apply_internal(s, a, !timed(s))
}
fn apply_internal(s: &State, a: &Action, record_history: bool) -> State {
    let mut n = State {
        rules_version: s.rules_version.clone(), radius: s.radius,
        board: s.board.clone(), turn: s.turn, reserves: s.reserves,
        ply: s.ply,
        history: if record_history { s.history.clone() } else { Vec::new() },
    };
    if record_history { n.history.push(key(s)); }
    n.ply += 1;
    if a.kind == "pass" {
        // V13: nothing on the board changes; only ply and turn advance.
    } else if a.kind == "rebirth" {
        let to = a.to.unwrap();
        n.reserves[s.turn as usize] -= 1;
        n.board.push(Piece {
            q: to[0],
            r: to[1],
            kind: Kind::Revenant,
            side: s.turn,
            facing: a.facing.unwrap(),
        });
    } else {
        let i = at(&n, a.from.unwrap()).unwrap();
        match a.kind.as_ref() {
            "rotate" => n.board[i].facing = a.facing.unwrap(),
            "swap" => {
                let j = at(&n, a.to.unwrap()).unwrap();
                let p = [n.board[i].q, n.board[i].r];
                let to = a.to.unwrap();
                n.board[i].q = to[0];
                n.board[i].r = to[1];
                n.board[j].q = p[0];
                n.board[j].r = p[1];
                if matches!(s.rules_version.as_str(), "hexwar-lab-v9" | "hexwar-lab-v10" | "hexwar-lab-v11" | "hexwar-lab-v12" | "hexwar-lab-v12-fan" | "hexwar-lab-v13" | "hexwar-lab-v14") {
                    n.board[j].facing = a.facing.unwrap();
                }
            }
            "push" => {
                let to = a.to.unwrap();
                let from = a.from.unwrap();
                let j = at(&n, to).unwrap();
                let len = distance(to[0] - from[0], to[1] - from[1]);
                n.board[j].q += (to[0] - from[0]) / len;
                n.board[j].r += (to[1] - from[1]) / len;
                n.board[i].q = to[0];
                n.board[i].r = to[1];
            }
            "move" => {
                let to = a.to.unwrap();
                let target = at(&n, to);
                let ghost = target.is_some_and(|j| n.board[j].kind == Kind::Ghost);
                if let Some(j) = target {
                    if n.board[j].kind == Kind::Revenant {
                        n.reserves[n.board[j].side as usize] += 1;
                    }
                }
                if ghost && n.board[i].kind == Kind::Revenant {
                    n.reserves[n.board[i].side as usize] += 1;
                }
                n.board[i].q = to[0];
                n.board[i].r = to[1];
                n.board = n
                    .board
                    .into_iter()
                    .enumerate()
                    .filter_map(|(j, p)| {
                        if Some(j) == target || (ghost && j == i) {
                            None
                        } else {
                            Some(p)
                        }
                    })
                    .collect();
            }
            _ => unreachable!(),
        }
    }
    n.turn = 1 - s.turn;
    canon(&mut n);
    n
}
fn value(k: Kind) -> i32 {
    match k {
        Kind::King | Kind::CrownedLancer => 0,
        Kind::Guard => 100,
        Kind::Scout => 90,
        Kind::Lancer => 180,
        Kind::Frog => 240,
        Kind::Queen => 550,
        Kind::Revenant => 160,
        Kind::Ghost => 180,
        Kind::Ram => 260,
        Kind::Warper => 180,
        // V12 priors are deliberately conservative. Rooting and enemy
        // exchange value is position-dependent; test positions, not taste,
        // must tune these before an army result is trusted.
        Kind::Enchanter => 220,
        Kind::Exchanger => 240,
        // V14 provisional engine prior (Warper 180 + unrestricted reach);
        // not a balance claim.
        Kind::Universal => 300,
        Kind::Cleaver => 200,
        // Provisional geometry families, not learned exchange prices. Existing
        // defaults remain frozen. Pawn/flankers scale Scout90 by ray count;
        // larger fans interpolate toward Cleaver200/Courser300/Queen550;
        // forward-only Strider120/Lancer180/Pike240 retain reach premiums;
        // jump arcs
        // scale Frog240 by full-ring destination count (5/12, 7/12, 18/12).
        Kind::Pawn => 30, Kind::Crab => 90, Kind::Flanker => 60,
        Kind::Strider => 120, Kind::Dancer => 133, Kind::Hound => 150,
        Kind::Triton => 150, Kind::Dragoon => 250, Kind::Courser => 300,
        Kind::Pike => 240, Kind::Rook => 300, Kind::Bishop => 450,
        Kind::Chariot => 375, Kind::Triskelion => 375,
        Kind::Knight => 100, Kind::Locust => 140, Kind::Cricket => 360,
        Kind::CharmedStrider => 145,
    }
}
#[derive(Clone)]
struct MaterialValues([i32; 33], Option<i32>, [i32; 7]);
const WEIGHT_NAMES: [&str; 7] = ["centrality", "king_centrality", "approach", "mobility", "pressure", "exposure", "urgency"];
const KINDS: [Kind; 33] = [
    Kind::King,
    Kind::Guard,
    Kind::Scout,
    Kind::Lancer,
    Kind::Frog,
    Kind::Queen,
    Kind::Revenant,
    Kind::Ghost,
    Kind::Ram,
    Kind::Warper,
    Kind::Cleaver,
    Kind::Pawn, Kind::Crab, Kind::Flanker, Kind::Strider, Kind::Dancer,
    Kind::Hound, Kind::Triton, Kind::Dragoon, Kind::Courser, Kind::Pike,
    Kind::Rook, Kind::Bishop, Kind::Chariot, Kind::Triskelion, Kind::Knight,
    Kind::Locust, Kind::Cricket, Kind::CrownedLancer, Kind::CharmedStrider,
    Kind::Enchanter, Kind::Exchanger, Kind::Universal,
];
impl Default for MaterialValues {
    fn default() -> Self {
        Self(KINDS.map(value), None, [100; 7])
    }
}
impl MaterialValues {
    fn reserve(&self) -> i32 {
        self.1.unwrap_or(self.get(Kind::Revenant) * 3 / 8)
    }
    fn weighted(&self, index: usize, value: i32) -> i32 {
        value * self.2[index] / 100
    }
    fn weights_json(&self) -> Value {
        let mut result = serde_json::Map::new();
        for (name, value) in WEIGHT_NAMES.iter().zip(self.2) {
            result.insert((*name).into(), json!(value));
        }
        Value::Object(result)
    }
    fn get(&self, k: Kind) -> i32 {
        self.0[k as usize]
    }
    fn parse(request: &Value) -> Result<Self, String> {
        let mut values = Self::default();
        if let Some(raw) = request.get("material_values") {
            for (name, raw_value) in raw.as_object().ok_or("material_values must be an object")? {
                let kind: Kind = serde_json::from_value(json!(name))
                    .map_err(|_| format!("unknown material piece {name}"))?;
                if royal(kind) {
                    return Err("king value cannot be overridden".into());
                }
                let v = raw_value
                    .as_i64()
                    .ok_or("material values must be integers 0..2000")?;
                if !(0..=2000).contains(&v) {
                    return Err("material values must be integers 0..2000".into());
                }
                values.0[kind as usize] = v as i32;
            }
        }
        if let Some(raw) = request.get("reserve_value") {
            let n = raw.as_i64().ok_or("reserve_value must be integer 0..2000")?;
            if !(0..=2000).contains(&n) { return Err("reserve_value must be integer 0..2000".into()); }
            values.1 = Some(n as i32);
        }
        if let Some(raw) = request.get("eval_weights") {
            for (name, raw_value) in raw.as_object().ok_or("eval_weights must be an object")? {
                let index = WEIGHT_NAMES.iter().position(|x| *x == name).ok_or_else(|| format!("unknown eval weight {name}"))?;
                let n = raw_value.as_i64().ok_or("eval weights must be integers 0..400")?;
                if !(0..=400).contains(&n) { return Err("eval weights must be integers 0..400".into()); }
                values.2[index] = n as i32;
            }
        }
        Ok(values)
    }
    fn json(&self) -> Value {
        let mut result = serde_json::Map::new();
        for k in KINDS.into_iter().filter(|k| !royal(*k)) {
            result.insert(
                serde_json::to_value(k).unwrap().as_str().unwrap().into(),
                json!(self.get(k)),
            );
        }
        Value::Object(result)
    }
}
#[cfg(test)]
fn evaluation(s: &State, policy: &str) -> i32 {
    evaluation_with(s, policy, &MaterialValues::default())
}
fn evaluation_with(s: &State, policy: &str, values: &MaterialValues) -> i32 {
    let mut score = [0, 0];
    let mut kings = [None, None];
    for p in &s.board {
        if royal(p.kind) { kings[p.side as usize] = Some(p); }
    }
    for p in &s.board {
        let side = p.side as usize;
        score[side] += values.get(p.kind);
        if policy == "positional" || policy == "tactical" {
            let central = s.radius - distance(p.q, p.r);
            score[side] += if royal(p.kind) {
                values.weighted(1, -central * 7)
            } else {
                values.weighted(0, central * 8)
            };
            if !royal(p.kind) {
                if let Some(k) = kings[(1 - p.side) as usize]
                {
                    score[side] += values.weighted(2, (s.radius * 2 - distance(p.q - k.q, p.r - k.r)) * 3);
                }
            }
        }
    }
    for i in 0..2 {
        score[i] += s.reserves[i] as i32 * values.reserve();
    }
    if policy == "tactical" {
        let activity = tactical_activity(s, values);
        score[0] += activity[0];
        score[1] += activity[1];
    }
    score[s.turn as usize] - score[(1 - s.turn) as usize]
}

fn tactical_activity(s: &State, values: &MaterialValues) -> [i32; 2] {
    // All weights are small relative to a 100-point guard. This is an explicit
    // heuristic hypothesis, not a rule change or a hidden legal-move filter.
    // Rotation itself earns nothing; its resulting reach can improve mobility.
    let mut score = [0, 0];
    let occupancy = Occupancy::new(s);
    for side in 0..2 {
        let enemy_king = s
            .board
            .iter()
            .find(|p| royal(p.kind) && p.side != side);
        let mut exposure = [0; 121];
        let mut pressure = [0; 121];
        for_each_move(s, false, side, |a| {
            let from = occupancy.at(a.from.unwrap()).unwrap();
            let attacker = &s.board[from];
            let to = a.to.unwrap();
            let victim = occupancy.at(to);
            let poisoned =
                a.kind == "move" && victim.is_some_and(|j| s.board[j].kind == Kind::Ghost);
            // A king may legally suicide into a Ghost, but this is neither useful
            // mobility nor pressure. Other poison exchanges are priced net.
            if poisoned && royal(attacker.kind) {
                return;
            }
            score[side as usize] += values.weighted(3, 2);
            if let Some(k) = enemy_king {
                let d = distance(to[0] - k.q, to[1] - k.r);
                if a.kind == "move" && !poisoned {
                    let p = if d == 0 {
                        40
                    } else if d == 1 {
                        8
                    } else if d == 2 {
                        3
                    } else {
                        0
                    };
                    pressure[from] = pressure[from].max(p);
                } else if a.kind == "push" && d == 0 {
                    // Displacing the king is useful but is not a capture threat.
                    pressure[from] = pressure[from].max(8);
                }
            }
            if a.kind == "move" {
                if let Some(j) = victim {
                    if !royal(s.board[j].kind) {
                        let loss = if poisoned {
                            values.get(attacker.kind)
                        } else {
                            0
                        };
                        let gain = (values.get(s.board[j].kind) - loss).max(0);
                        exposure[j] = exposure[j].max(gain.min(240) / 12);
                    }
                }
            }
        });
        score[side as usize] += values.weighted(4, pressure[..s.board.len()].iter().sum::<i32>()) + values.weighted(5, exposure[..s.board.len()].iter().sum::<i32>());
    }
    score
}
struct Search {
    material_values: MaterialValues,
    nodes: u64,
    budget: u64,
    policy: String,
    urgency: bool,
    prefer_short_wins: bool,
    quiescence: bool,
    q_depth: u32,
    q_nodes: u64,
    q_cap_hits: u64,
    max_q_ply: u32,
}

#[cfg(test)]
fn search_evaluation(s: &State, policy: &str, urgency: bool) -> i32 {
    search_evaluation_with(s, policy, urgency, &MaterialValues::default())
}
fn search_evaluation_with(s: &State, policy: &str, urgency: bool, values: &MaterialValues) -> i32 {
    let base = evaluation_with(s, policy, values) as i64;
    let value = if urgency && timed(s) {
        let p2 = (s.ply * s.ply) as i64;
        let mut d = [0, 0];
        for p in s.board.iter().filter(|p| royal(p.kind)) {
            d[p.side as usize] = distance(p.q, p.r);
        }
        let center_adv = (d[(1 - s.turn) as usize] - d[s.turn as usize]) as i64;
        if matches!(s.rules_version.as_str(), "hexwar-lab-v6" | "hexwar-lab-v7" | "hexwar-lab-v8" | "hexwar-lab-v9" | "hexwar-lab-v10" | "hexwar-lab-v11" | "hexwar-lab-v12" | "hexwar-lab-v12-fan" | "hexwar-lab-v13" | "hexwar-lab-v14") {
            let total = (deadline(s) * deadline(s)) as i64;
            let counts = body_counts(s);
            let body_adv = counts[s.turn as usize] - counts[(1 - s.turn) as usize];
            // A board has at most 1+3r(r+1) bodies. Entire count advantage
            // is therefore less than one center hex, even on radius five.
            let cells = (1 + 3 * s.radius * (s.radius + 1)) as i64;
            let center_weight = values.weighted(6, 600) as i64;
            if matches!(s.rules_version.as_str(), "hexwar-lab-v7" | "hexwar-lab-v8" | "hexwar-lab-v9" | "hexwar-lab-v10" | "hexwar-lab-v11" | "hexwar-lab-v12" | "hexwar-lab-v12-fan" | "hexwar-lab-v13" | "hexwar-lab-v14") {
                let distances = army_distances(s);
                let army_adv = distances[(1 - s.turn) as usize] - distances[s.turn as usize];
                // Strict global bound: tertiary < one body; all lower-order
                // advantages together < one king-distance step. Divide once
                // after blending so fractional signals are not lost early.
                let bound = cells * s.radius as i64 + 1;
                let scale = cells * bound;
                let objective = center_weight * (center_adv * scale + body_adv * bound + army_adv);
                (base * (total - p2) * scale + objective * p2) / (total * scale)
            } else {
                let objective = center_weight * center_adv + center_weight * body_adv / cells;
                (base * (total - p2) + objective * p2) / total
            }
        } else {
            (base * (10000 - p2) + values.weighted(6, 600) as i64 * p2 * center_adv) / 10000
        }
    } else {
        base
    };
    value.clamp(-(WIN as i64) / 2, (WIN as i64) / 2) as i32
}

// Direct attack geometry excludes rotations as current attacks and keeps the
// distinction between captures, Ram displacement, and suicidal Ghost trades.
fn can_capture(s: &State, p: &Piece, to: [i32; 2]) -> bool {
    let dq = to[0] - p.q;
    let dr = to[1] - p.r;
    let len = distance(dq, dr);
    if len == 0 {
        return false;
    }
    if matches!(p.kind, Kind::Ram | Kind::Warper | Kind::Enchanter | Kind::Exchanger | Kind::Universal) || rooted(s, p) {
        return false;
    }
    if let Some(range) = jump_range(p.kind) {
        return len == range && jump_allowed(p.kind, p.facing, dq, dr);
    }
    if matches!(p.kind, Kind::King | Kind::Guard | Kind::Ghost) {
        return len == 1;
    }
    let Some(dir) = DIRS
        .iter()
        .position(|v| dq == v[0] * len && dr == v[1] * len)
    else {
        return false;
    };
    if !ray_allowed(p.kind, p.facing, dir) || len > state_ray_limit(s, p.kind, p.facing, dir) {
        return false;
    }
    let v = DIRS[dir];
    (1..len).all(|i| at(s, [p.q + i * v[0], p.r + i * v[1]]).is_none())
}
fn king_attacked(s: &State, side: u8) -> bool {
    s.board
        .iter()
        .find(|p| royal(p.kind) && p.side == side)
        .is_some_and(|k| {
            s.board
                .iter()
                .any(|p| p.side != side && can_capture(s, p, [k.q, k.r]))
        })
}
fn material_attacked(s: &State, to: [i32; 2], side: u8, values: &MaterialValues) -> bool {
    let Some(i) = at(s, to) else {
        return false;
    };
    let victim = &s.board[i];
    if victim.side == side || royal(victim.kind) {
        return false;
    }
    s.board.iter().any(|p| {
        p.side == side
            && can_capture(s, p, to)
            && (victim.kind != Kind::Ghost
                || (!royal(p.kind) && values.get(victim.kind) > values.get(p.kind)))
    })
}
#[cfg(test)]
fn q_forcing(s: &State, a: &Action, child: &State, already_threatens_king: bool) -> bool {
    q_forcing_with(
        s,
        a,
        child,
        already_threatens_king,
        &MaterialValues::default(),
    )
}
fn q_forcing_with(
    s: &State,
    a: &Action,
    child: &State,
    already_threatens_king: bool,
    values: &MaterialValues,
) -> bool {
    if a.kind == "move" && a.to.is_some_and(|to| at(s, to).is_some()) {
        return true;
    }
    if !already_threatens_king && king_attacked(child, 1 - s.turn) {
        return true;
    }
    // V12 contact is an immediately visible forced-response event.  Without
    // this, a shallow Q leaf treats an Enchanter stepping into a lock as a
    // quiet Scout move and habitually stops one ply before the punishment.
    if rooted_count(child, 1 - s.turn) > rooted_count(s, 1 - s.turn) {
        return true;
    }
    if a.kind == "swap" {
        // A swap has no capture target, but can newly expose a non-royal
        // enemy to ordinary attack.  This covers both enemy Exchanger swaps
        // and the existing friendly Warper's tactical rearrangements.
        return child.board.iter().any(|p| {
            p.side != s.turn
                && !royal(p.kind)
                && material_attacked(child, [p.q, p.r], s.turn, values)
                && !material_attacked(s, [p.q, p.r], s.turn, values)
        });
    }
    if a.kind == "push" {
        let to = a.to.unwrap();
        if at(s, to).is_some_and(|i| s.board[i].side == s.turn) {
            // Friendly delivery or vacating a ray can create a material threat.
            return child.board.iter().any(|p| {
                p.side != s.turn
                    && material_attacked(child, [p.q, p.r], s.turn, values)
                    && !material_attacked(s, [p.q, p.r], s.turn, values)
            });
        }
        let from = a.from.unwrap();
        let len = distance(to[0] - from[0], to[1] - from[1]);
        let beyond = [
            to[0] + (to[0] - from[0]) / len,
            to[1] + (to[1] - from[1]) / len,
        ];
        return !material_attacked(s, to, s.turn, values)
            && material_attacked(child, beyond, s.turn, values);
    }
    false
}
impl Search {
    fn terminal_value(&self, s: &State, t: &Outcome, root_ply: u32) -> i32 {
        let magnitude = WIN
            - if self.prefer_short_wins {
                root_ply as i32
            } else {
                0
            };
        match t.winner {
            Some(w) if w == s.turn => magnitude,
            Some(_) => -magnitude,
            None => 0,
        }
    }
    fn visit(
        &mut self,
        s: &State,
        depth: u32,
        mut alpha: i32,
        beta: i32,
        root_ply: u32,
    ) -> Result<i32, ()> {
        if depth == 0 && self.quiescence {
            return self.qvisit(s, self.q_depth, alpha, beta, root_ply, 0);
        }
        if self.nodes >= self.budget {
            return Err(());
        }
        self.nodes += 1;
        let t = terminal(s);
        if t.status != "ongoing" {
            return Ok(self.terminal_value(s, &t, root_ply));
        }
        let mut actions = moves(s);
        if actions.is_empty() {
            return Ok(0);
        }
        if depth == 0 {
            return Ok(search_evaluation_with(
                s,
                &self.policy,
                self.urgency,
                &self.material_values,
            ));
        }
        order(s, &mut actions, &self.material_values);
        let mut best = -WIN - 1;
        for a in actions {
            let n = apply_search(s, &a);
            let v = -self.visit(&n, depth - 1, -beta, -alpha, root_ply + 1)?;
            best = best.max(v);
            alpha = alpha.max(v);
            if alpha >= beta {
                break;
            }
        }
        Ok(best)
    }
    fn qvisit(
        &mut self,
        s: &State,
        remaining: u32,
        mut alpha: i32,
        beta: i32,
        root_ply: u32,
        q_ply: u32,
    ) -> Result<i32, ()> {
        if self.nodes >= self.budget {
            return Err(());
        }
        self.nodes += 1;
        self.q_nodes += 1;
        self.max_q_ply = self.max_q_ply.max(q_ply);
        let t = terminal(s);
        if t.status != "ongoing" {
            return Ok(self.terminal_value(s, &t, root_ply));
        }
        let mut actions = moves(s);
        if actions.is_empty() {
            return Ok(0);
        }
        // In a forced-response node stand-pat is forbidden. Its static score
        // is unused unless we hit the extension cap, so do not compute it.
        let all_responses = remaining > 0
            && (king_attacked(s, s.turn) || (timed(s) && deadline(s) - s.ply <= remaining as u64));
        let static_value = if remaining == 0 || !all_responses {
            search_evaluation_with(s, &self.policy, self.urgency, &self.material_values)
        } else { 0 };
        if remaining == 0 {
            self.q_cap_hits += 1;
            return Ok(static_value);
        }
        // Near the real deadline, hypothetical stand-pat would freeze the clock.
        // Search every action if this extension can reach that deadline. Outside
        // this region stand-pat still assumes no zugzwang: explicitly heuristic.
        let already_threatens = king_attacked(s, 1 - s.turn);
        let mut best = if all_responses {
            -WIN - 1
        } else {
            static_value
        };
        if !all_responses {
            if best >= beta {
                return Ok(best);
            }
            alpha = alpha.max(best);
        }
        order(s, &mut actions, &self.material_values);
        for a in actions {
            let child = apply_search(s, &a);
            if !all_responses
                && !q_forcing_with(s, &a, &child, already_threatens, &self.material_values)
            {
                continue;
            }
            let v = -self.qvisit(
                &child,
                remaining - 1,
                -beta,
                -alpha,
                root_ply + 1,
                q_ply + 1,
            )?;
            best = best.max(v);
            alpha = alpha.max(v);
            if alpha >= beta {
                break;
            }
        }
        Ok(best)
    }
}
fn order(s: &State, a: &mut [Action], values: &MaterialValues) {
    let occupancy = Occupancy::new(s);
    a.sort_by_cached_key(|a| {
        let target = a.to.and_then(|p| occupancy.at(p));
        let score = if a.kind == "move" {
            target.map_or(0, |i| {
                if royal(s.board[i].kind) {
                    WIN
                } else {
                    values.get(s.board[i].kind)
                }
            })
        } else if a.kind == "swap" {
            target.filter(|&i| s.board[i].side != s.turn)
                .map_or(0, |i| values.get(s.board[i].kind))
        } else if a.kind == "rebirth" {
            values.get(Kind::Revenant) * 5 / 16
        } else if a.kind == "rotate" {
            -20
        } else {
            0
        };
        -score
    });
}
fn tie(seed: u64, index: usize) -> u64 {
    let mut x = seed
        .wrapping_add(index as u64)
        .wrapping_add(0x9e3779b97f4a7c15);
    x = (x ^ (x >> 30)).wrapping_mul(0xbf58476d1ce4e5b9);
    x = (x ^ (x >> 27)).wrapping_mul(0x94d049bb133111eb);
    x ^ (x >> 31)
}
fn get_state(v: &Value) -> Result<State, String> {
    let mut s: State = serde_json::from_value(v.clone()).map_err(|e| e.to_string())?;
    validate(&mut s)?;
    Ok(s)
}
fn bool_option(v: &Value, name: &str, default: bool) -> Result<bool, String> {
    match v.get(name) {
        None => Ok(default),
        Some(x) => x.as_bool().ok_or_else(|| format!("{name} must be boolean")),
    }
}
fn process(v: Value) -> Result<Value, String> {
    let mut response = process_inner(v.clone())?;
    if matches!(v["op"].as_str(), Some("search" | "evaluate")) {
        let values = MaterialValues::parse(&v)?;
        response["reserve_value"] = json!(values.reserve());
        response["eval_weights"] = values.weights_json();
        let mut config = json!({
            "policy": v["policy"].as_str().unwrap_or("material"),
            "urgency": v["urgency"].as_bool().unwrap_or(v["state"]["rules_version"] != "hexwar-lab-v1"),
            "prefer_short_wins": v["prefer_short_wins"].as_bool().unwrap_or(true),
            "quiescence": v["quiescence"].as_bool().unwrap_or(false),
            "q_depth": v["q_depth"].as_u64().unwrap_or(4),
            "material_values": values.json(),
            "reserve_value": values.reserve(),
            "eval_weights": values.weights_json()
        });
        if v["op"] == "search" {
            config["depth"] = v["depth"].clone();
            config["node_budget"] = json!(v["node_budget"].as_u64().unwrap_or(100000));
            config["seed"] = json!(v["seed"].as_u64().unwrap_or(0));
        }
        response["effective_config"] = config;
        // Preserve v1-v4 response fingerprints as well as their move/eval
        // behavior: historical material manifests contained only eleven kinds.
        if !matches!(v["state"]["rules_version"].as_str(), Some("hexwar-lab-v5" | "hexwar-lab-v6" | "hexwar-lab-v7" | "hexwar-lab-v8" | "hexwar-lab-v9" | "hexwar-lab-v10" | "hexwar-lab-v11" | "hexwar-lab-v12" | "hexwar-lab-v12-fan" | "hexwar-lab-v13" | "hexwar-lab-v14")) {
            for path in ["/material_values", "/effective_config/material_values"] {
                if let Some(Value::Object(map)) = response.pointer_mut(path) {
                    for k in &KINDS[11..] {
                        map.remove(serde_json::to_value(k).unwrap().as_str().unwrap());
                    }
                }
            }
        }
        // V10 adds one overridable ordinary value. Earlier versioned
        // responses must not acquire it merely because the executable is new.
        if !matches!(v["state"]["rules_version"].as_str(), Some("hexwar-lab-v10" | "hexwar-lab-v11" | "hexwar-lab-v12" | "hexwar-lab-v12-fan" | "hexwar-lab-v13" | "hexwar-lab-v14")) {
            for path in ["/material_values", "/effective_config/material_values"] {
                if let Some(Value::Object(map)) = response.pointer_mut(path) {
                    for k in [Kind::CrownedLancer, Kind::CharmedStrider] {
                        map.remove(serde_json::to_value(k).unwrap().as_str().unwrap());
                    }
                }
            }
        }
        // V12 adds the two Fey positional specialists. Keep historical
        // response fingerprints/material contracts exact for V1--V11.
        if !matches!(v["state"]["rules_version"].as_str(), Some("hexwar-lab-v12" | "hexwar-lab-v12-fan" | "hexwar-lab-v13" | "hexwar-lab-v14")) {
            for path in ["/material_values", "/effective_config/material_values"] {
                if let Some(Value::Object(map)) = response.pointer_mut(path) {
                    for k in [Kind::Enchanter, Kind::Exchanger] {
                        map.remove(serde_json::to_value(k).unwrap().as_str().unwrap());
                    }
                }
            }
        }
        // V14 adds the Universal; V1--V13 material contracts stay exact.
        if v["state"]["rules_version"].as_str() != Some("hexwar-lab-v14") {
            for path in ["/material_values", "/effective_config/material_values"] {
                if let Some(Value::Object(map)) = response.pointer_mut(path) {
                    map.remove("universal");
                }
            }
        }
    }
    Ok(response)
}
fn process_inner(v: Value) -> Result<Value, String> {
    let op = v["op"].as_str().ok_or("missing op")?;
    if op == "catalog" {
        // Catalog had no version selector before V10. Preserve its V9 response
        // by default; callers asking about the new Fey grammar opt in directly.
        let catalog_version = v["rules_version"].as_str().unwrap_or("hexwar-lab-v9");
        if !matches!(catalog_version, "hexwar-lab-v9" | "hexwar-lab-v10" | "hexwar-lab-v11" | "hexwar-lab-v12" | "hexwar-lab-v12-fan" | "hexwar-lab-v13" | "hexwar-lab-v14") {
            return Err("catalog rules_version must be hexwar-lab-v9, hexwar-lab-v10, hexwar-lab-v11, hexwar-lab-v12, hexwar-lab-v12-fan, hexwar-lab-v13, or hexwar-lab-v14".into());
        }
        let kinds = if catalog_version == "hexwar-lab-v14" { &KINDS[..] }
            else if matches!(catalog_version, "hexwar-lab-v12" | "hexwar-lab-v12-fan" | "hexwar-lab-v13") { &KINDS[..32] }
            else if matches!(catalog_version, "hexwar-lab-v10" | "hexwar-lab-v11") { &KINDS[..30] }
            else { &KINDS[..28] };
        let pieces: Vec<_> = kinds.iter().map(|&k| json!({
            "kind": k, "directional": directional(k) || (matches!(catalog_version, "hexwar-lab-v11" | "hexwar-lab-v12" | "hexwar-lab-v12-fan" | "hexwar-lab-v13" | "hexwar-lab-v14") && k == Kind::Queen), "default_value": value(k),
            "min_rules": if k == Kind::Universal {"hexwar-lab-v14"} else if matches!(k, Kind::Enchanter | Kind::Exchanger) {"hexwar-lab-v12"} else if matches!(k, Kind::CrownedLancer | Kind::CharmedStrider) {"hexwar-lab-v10"} else if k as usize > Kind::Cleaver as usize {"hexwar-lab-v5"} else if k == Kind::Cleaver {"hexwar-lab-v4"} else {"hexwar-lab-v1"},
            "movement": if jump_range(k).is_some() {"jump"} else if matches!(k, Kind::Warper | Kind::Exchanger | Kind::Universal) {"swap"} else if k==Kind::Ram {"push"} else {"ray"},
            "range": jump_range(k).unwrap_or_else(||ray_range(k,5)),
            "unlimited": jump_range(k).is_none() && ray_range(k,5)==10,
            "relative_dirs": if jump_range(k).is_some() {Vec::new()} else {(0..6).filter(|&d|ray_allowed(k,0,d)).collect::<Vec<_>>()},
            "forward_arc_degrees": if matches!(k,Kind::Knight|Kind::Locust) {Some(150)} else {None},
        })).collect();
        return Ok(json!({"ok":true,"rules_version":catalog_version,"pieces":pieces,"aliases":{"ranger":"cleaver"}}));
    }
    let s = get_state(&v["state"])?;
    match op {
        "legal" => {
            let o = outcome(&s);
            let a = if o.status == "ongoing" {
                moves(&s)
            } else {
                vec![]
            };
            Ok(json!({"ok":true,"actions":a,"outcome":o}))
        }
        "apply" => {
            let a: Action =
                serde_json::from_value(v["action"].clone()).map_err(|e| e.to_string())?;
            if outcome(&s).status != "ongoing" || !moves(&s).contains(&a) {
                return Err("illegal action".into());
            }
            let n = apply_unchecked(&s, &a);
            let o = outcome(&n);
            Ok(json!({"ok":true,"state":n,"outcome":o}))
        }
        "evaluate" | "search" => {
            let material_values = MaterialValues::parse(&v)?;
            let policy = v["policy"].as_str().unwrap_or("material");
            if !["material", "positional", "tactical"].contains(&policy) {
                return Err("unknown policy".into());
            }
            let urgency = bool_option(&v, "urgency", timed(&s))?;
            let prefer_short_wins = bool_option(&v, "prefer_short_wins", true)?;
            let quiescence = bool_option(&v, "quiescence", false)?;
            let q_depth = match v.get("q_depth") {
                None => 4,
                Some(x) => x.as_u64().ok_or("q_depth must be an integer")?,
            };
            if q_depth > 8 {
                return Err("q_depth must be 0..8".into());
            }
            if op == "evaluate" {
                let t = outcome(&s);
                let score = if t.status == "ongoing" {
                    search_evaluation_with(&s, policy, urgency, &material_values)
                } else {
                    match t.winner {
                        Some(w) if w == s.turn => WIN,
                        Some(_) => -WIN,
                        None => 0,
                    }
                };
                return Ok(
                    json!({"ok":true,"value":score,"material_values":material_values.json()}),
                );
            }
            let depth = v["depth"].as_u64().ok_or("depth required")?;
            if !(1..=12).contains(&depth) {
                return Err("depth must be 1..12".into());
            }
            let budget = v["node_budget"].as_u64().unwrap_or(100000);
            if budget == 0 {
                return Err("node_budget must be positive".into());
            }
            let seed = v["seed"].as_u64().unwrap_or(0);
            let o = outcome(&s);
            if o.status != "ongoing" {
                let val = match o.winner {
                    Some(w) if w == s.turn => WIN,
                    Some(_) => -WIN,
                    None => 0,
                };
                return Ok(
                    json!({"ok":true,"action":null,"value":val,"nodes":0,"completed_depth":0,"budget_exhausted":false,"q_nodes":0,"q_cap_hits":0,"max_q_ply":0,"material_values":material_values.json()}),
                );
            }
            let mut actions = moves(&s);
            order(&s, &mut actions, &material_values);
            let mut chosen = actions[0].clone();
            let mut result = search_evaluation_with(&s, policy, urgency, &material_values);
            let mut completed = 0;
            let mut exhausted = false;
            let mut search = Search {
                material_values,
                nodes: 0,
                budget,
                policy: policy.into(),
                urgency,
                prefer_short_wins,
                quiescence,
                q_depth: q_depth as u32,
                q_nodes: 0,
                q_cap_hits: 0,
                max_q_ply: 0,
            };
            for d in 1..=depth as u32 {
                let mut best = -WIN - 1;
                let mut winner = 0;
                let mut success = true;
                // Full windows at root preserve exact equal-score ties (no bound masquerading as equality).
                for (i, a) in actions.iter().enumerate() {
                    let n = apply_search(&s, a);
                    match search.visit(&n, d - 1, -WIN - 1, WIN + 1, 1) {
                        Ok(x) => {
                            let val = -x;
                            if val > best || (val == best && tie(seed, i) > tie(seed, winner)) {
                                best = val;
                                winner = i;
                            }
                        }
                        Err(()) => {
                            success = false;
                            break;
                        }
                    }
                }
                if !success {
                    exhausted = true;
                    break;
                }
                chosen = actions[winner].clone();
                result = best;
                completed = d;
            }
            Ok(
                json!({"ok":true,"action":chosen,"value":result,"nodes":search.nodes,"completed_depth":completed,"budget_exhausted":exhausted,"q_nodes":search.q_nodes,"q_cap_hits":search.q_cap_hits,"max_q_ply":search.max_q_ply,"material_values":search.material_values.json()}),
            )
        }
        _ => Err("unknown operation".into()),
    }
}
fn main() {
    let stdin = io::stdin();
    let mut stdout = io::BufWriter::new(io::stdout().lock());
    for line in stdin.lock().lines() {
        let result = match line {
            Ok(line) => serde_json::from_str(&line)
                .map_err(|e| e.to_string())
                .and_then(process),
            Err(e) => Err(e.to_string()),
        };
        let response = result.unwrap_or_else(|e| json!({"ok":false,"error":e}));
        if writeln!(stdout, "{}", response)
            .and_then(|_| stdout.flush())
            .is_err()
        {
            break;
        }
    }
}

#[cfg(test)]
mod friendly_tests;
#[cfg(test)]
mod tests;
#[cfg(test)]
mod timed_tests;
#[cfg(test)]
mod weekend_tests;
#[cfg(test)]
mod throughput_tests;
#[cfg(test)]
mod catalog_tests;
#[cfg(test)]
mod round35_tests;
#[cfg(test)]
mod army_proximity_tests;
#[cfg(test)]
mod round42_tests;
#[cfg(test)]
mod fan_tests;
#[cfg(test)]
mod pass_tests;
#[cfg(test)]
mod universal_tests;
