use super::*;
fn p(q: i32, r: i32, k: Kind, side: u8) -> Piece {
    Piece {
        q,
        r,
        kind: k,
        side,
        facing: if side == 0 { 0 } else { 3 },
    }
}
fn state(mut pieces: Vec<Piece>) -> State {
    pieces.extend([p(0, 3, Kind::King, 0), p(0, -3, Kind::King, 1)]);
    let mut s = State {
        rules_version: "hexwar-lab-v1".into(),
        radius: 3,
        board: pieces,
        turn: 0,
        reserves: [0, 0],
        ply: 0,
        history: vec![],
    };
    validate(&mut s).unwrap();
    s
}
fn play(s: &State, k: &str, f: Option<[i32; 2]>, t: Option<[i32; 2]>, face: Option<u8>) -> State {
    let a = action(k, f, t, face);
    assert!(moves(s).contains(&a), "illegal test action {:?}", a);
    apply_unchecked(s, &a)
}
#[test]
fn ghost_capture_both_directions() {
    let s = state(vec![p(0, 0, Kind::Guard, 0), p(1, 0, Kind::Ghost, 1)]);
    let n = play(&s, "move", Some([0, 0]), Some([1, 0]), None);
    assert_eq!(n.board.len(), 2);
    let s = state(vec![p(0, 0, Kind::Ghost, 0), p(1, 0, Kind::Guard, 1)]);
    let n = play(&s, "move", Some([0, 0]), Some([1, 0]), None);
    assert_eq!(n.board.len(), 3);
    assert_eq!(n.board[at(&n, [1, 0]).unwrap()].kind, Kind::Ghost);
    let s = state(vec![p(0, 0, Kind::Ghost, 0), p(1, 0, Kind::Ghost, 1)]);
    assert_eq!(
        play(&s, "move", Some([0, 0]), Some([1, 0]), None)
            .board
            .len(),
        2
    );
}
#[test]
fn king_taking_ghost_loses() {
    let s = state(vec![p(1, 2, Kind::Ghost, 1)]);
    let n = play(&s, "move", Some([0, 3]), Some([1, 2]), None);
    assert_eq!(outcome(&n).winner, Some(1));
}
#[test]
fn revenants_multiple_and_rebirth_facing() {
    let mut s = state(vec![p(0, 0, Kind::Revenant, 0), p(0, -1, Kind::Ghost, 1)]);
    s.reserves = [2, 0];
    let mut n = play(&s, "move", Some([0, 0]), Some([0, -1]), None);
    assert_eq!(n.reserves, [3, 0]);
    n.turn = 0;
    let n = play(&n, "rebirth", None, Some([0, 2]), Some(3));
    assert_eq!(n.reserves, [2, 0]);
    assert_eq!(n.board[at(&n, [0, 2]).unwrap()].facing, 3);
    let s = state(vec![p(0, 0, Kind::Guard, 0), p(1, 0, Kind::Revenant, 1)]);
    assert_eq!(
        play(&s, "move", Some([0, 0]), Some([1, 0]), None).reserves,
        [0, 1]
    );
}
#[test]
fn ram_push_range_facing_and_blockers() {
    let mut enemy = p(1, 0, Kind::Ghost, 1);
    enemy.facing = 4;
    let s = state(vec![p(-2, 0, Kind::Ram, 0), enemy]);
    let n = play(&s, "push", Some([-2, 0]), Some([1, 0]), None);
    assert_eq!(n.board.len(), 4);
    assert_eq!(n.board[at(&n, [2, 0]).unwrap()].facing, 4);
    for blocker in [p(2, 0, Kind::Guard, 1), p(2, 0, Kind::Guard, 0)] {
        let s = state(vec![
            p(-2, 0, Kind::Ram, 0),
            p(1, 0, Kind::Guard, 1),
            blocker,
        ]);
        assert!(!moves(&s).iter().any(|a| a.kind == "push"));
    }
    let s = state(vec![p(0, 0, Kind::Ram, 0), p(3, 0, Kind::Guard, 1)]);
    assert!(!moves(&s).iter().any(|a| a.kind == "push"));
}
#[test]
fn ram_pushes_king_without_capture() {
    let mut s = state(vec![p(0, -1, Kind::Ram, 1)]);
    s.turn = 1;
    s.board
        .iter_mut()
        .find(|p| p.kind == Kind::King && p.side == 0)
        .unwrap()
        .r = 1;
    let n = play(&s, "push", Some([0, -1]), Some([0, 1]), None);
    assert_eq!(n.board[at(&n, [0, 2]).unwrap()].kind, Kind::King);
    assert_eq!(outcome(&n).status, "ongoing");
}
#[test]
fn warper_first_friendly_only() {
    let s = state(vec![
        p(-2, 0, Kind::Warper, 0),
        p(0, 0, Kind::Guard, 0),
        p(2, 0, Kind::Guard, 0),
    ]);
    let a = moves(&s);
    assert!(a.contains(&action("swap", Some([-2, 0]), Some([0, 0]), None)));
    assert!(!a
        .iter()
        .any(|a| a.from == Some([-2, 0]) && a.to == Some([2, 0])));
    assert!(!a
        .iter()
        .any(|a| a.from == Some([-2, 0]) && a.kind != "swap"));
    let n = play(&s, "swap", Some([-2, 0]), Some([0, 0]), None);
    assert_eq!(n.board[at(&n, [0, 0]).unwrap()].kind, Kind::Warper);
}
#[test]
fn v9_warper_swap_rotates_only_the_swapped_piece() {
    let mut s = state(vec![
        p(-2, 0, Kind::Warper, 0),
        p(0, 0, Kind::Lancer, 0),
    ]);
    s.rules_version = "hexwar-lab-v9".into();
    let warper_index = at(&s, [-2, 0]).unwrap();
    s.board[warper_index].facing = 2;
    let a = moves(&s);
    let swaps: Vec<_> = a.iter().filter(|a| a.kind == "swap"
        && a.from == Some([-2, 0]) && a.to == Some([0, 0])).collect();
    assert_eq!(swaps.len(), 6);
    assert!(swaps.iter().any(|a| a.facing == Some(4)));
    let n = play(&s, "swap", Some([-2, 0]), Some([0, 0]), Some(4));
    let warper = &n.board[at(&n, [0, 0]).unwrap()];
    let lancer = &n.board[at(&n, [-2, 0]).unwrap()];
    assert_eq!((warper.kind, warper.facing), (Kind::Warper, 2));
    assert_eq!((lancer.kind, lancer.facing), (Kind::Lancer, 4));
    assert!(!moves(&s).contains(&action("swap", Some([-2, 0]), Some([0, 0]), None)));
}
fn v10_state(mut pieces: Vec<Piece>) -> State {
    pieces.push(p(0, -4, Kind::King, 1));
    let mut s = State {
        rules_version: "hexwar-lab-v10".into(), radius: 4, board: pieces,
        turn: 0, reserves: [0, 0], ply: 0, history: vec![],
    };
    validate(&mut s).unwrap();
    s
}
fn v12_state(mut pieces: Vec<Piece>) -> State {
    pieces.push(p(0, -4, Kind::King, 1));
    let mut s = State {
        rules_version: "hexwar-lab-v12".into(), radius: 4, board: pieces,
        turn: 0, reserves: [0, 0], ply: 0, history: vec![],
    };
    validate(&mut s).unwrap();
    s
}
#[test]
fn v10_charmed_strider_has_scout_fan_and_forward_two_only() {
    let s = v10_state(vec![p(0, 2, Kind::King, 0), p(0, 0, Kind::CharmedStrider, 0)]);
    let a = moves(&s);
    assert!(a.contains(&action("move", Some([0, 0]), Some([0, -2]), None)));
    assert!(a.contains(&action("move", Some([0, 0]), Some([1, -1]), None)));
    assert!(!a.contains(&action("move", Some([0, 0]), Some([2, -2]), None)));
    assert_eq!(a.iter().filter(|x| x.kind == "rotate" && x.from == Some([0, 0])).count(), 5);
}
#[test]
fn v10_crowned_lancer_is_royal_and_has_one_step_omni_plus_forward_three() {
    let mut crown = p(0, 0, Kind::CrownedLancer, 0);
    crown.facing = 0;
    let s = v10_state(vec![crown]);
    let a = moves(&s);
    assert!(a.contains(&action("move", Some([0, 0]), Some([0, -3]), None)));
    assert!(a.contains(&action("move", Some([0, 0]), Some([1, 0]), None)));
    assert!(!a.contains(&action("move", Some([0, 0]), Some([2, 0]), None)));
    let without_crown = State { board: vec![p(0, -4, Kind::King, 1)], ..s.clone() };
    assert_eq!(outcome(&without_crown).winner, Some(1));
}
#[test]
fn v11_queen_has_forward_ray_and_one_step_halo() {
    let mut s = v10_state(vec![p(0, 2, Kind::King, 0), p(0, 0, Kind::Queen, 0)]);
    s.rules_version = "hexwar-lab-v11".into();
    validate(&mut s).unwrap();
    let a = moves(&s);
    // Directly forward remains a clear ray; sideways/backward directions are
    // the royal halo and therefore exactly one step.
    assert!(a.contains(&action("move", Some([0, 0]), Some([0, -3]), None)));
    assert!(a.contains(&action("move", Some([0, 0]), Some([1, 0]), None)));
    assert!(!a.contains(&action("move", Some([0, 0]), Some([2, 0]), None)));
    assert_eq!(a.iter().filter(|x| x.kind == "rotate" && x.from == Some([0, 0])).count(), 5);
}
#[test]
fn v12_enchanter_is_non_capturing_scout_and_roots_adjacent_enemy() {
    let mut s = v12_state(vec![
        p(0, 2, Kind::King, 0),
        p(0, 0, Kind::Enchanter, 0),
        // This is an allowed Scout-fan destination, so its absence verifies
        // the Enchanter's no-capture contract rather than ray geometry.
        p(1, -1, Kind::Scout, 1),
        p(3, 0, Kind::Guard, 1),
    ]);
    let own = moves(&s);
    assert!(own.contains(&action("move", Some([0, 0]), Some([0, -1]), None)));
    assert!(!own.contains(&action("move", Some([0, 0]), Some([1, -1]), None)));
    assert!(!can_capture(&s, &s.board[at(&s, [0, 0]).unwrap()], [1, -1]));
    s.turn = 1;
    let enemy = moves(&s);
    // The contacted Scout cannot move, capture, or rotate; the distant Guard
    // remains active, so this is a local spatial lock rather than a turn skip.
    assert!(!enemy.iter().any(|a| a.from == Some([1, -1])));
    assert!(enemy.iter().any(|a| a.from == Some([3, 0])));
    assert!(!can_capture(&s, &s.board[at(&s, [1, -1]).unwrap()], [0, 0]));
}
#[test]
fn v12_exchanger_swaps_first_enemy_on_any_range_three_ray() {
    let s = v12_state(vec![
        p(0, 2, Kind::King, 0),
        p(0, 0, Kind::Exchanger, 0),
        p(0, -2, Kind::Guard, 1),
        p(0, -3, Kind::Lancer, 1),
    ]);
    let exchanges: Vec<_> = moves(&s).into_iter()
        .filter(|a| a.from == Some([0, 0])).collect();
    assert_eq!(exchanges.len(), 6);
    assert!(exchanges.iter().all(|a| a.kind == "swap" && a.to == Some([0, -2])));
    let n = play(&s, "swap", Some([0, 0]), Some([0, -2]), Some(4));
    assert_eq!(n.board[at(&n, [0, -2]).unwrap()].kind, Kind::Exchanger);
    let guard = &n.board[at(&n, [0, 0]).unwrap()];
    assert_eq!((guard.kind, guard.facing), (Kind::Guard, 4));
    assert_eq!(n.board[at(&n, [0, -3]).unwrap()].kind, Kind::Lancer);
}
#[test]
fn v12_specialists_are_version_gated_and_catalogued() {
    let mut old = v12_state(vec![p(0, 2, Kind::King, 0), p(0, 0, Kind::Enchanter, 0)]);
    old.rules_version = "hexwar-lab-v11".into();
    assert!(validate(&mut old).is_err());
    let catalog = process(json!({"op":"catalog", "rules_version":"hexwar-lab-v12"})).unwrap();
    let pieces = catalog["pieces"].as_array().unwrap();
    assert!(pieces.iter().any(|p| p["kind"] == "enchanter" && p["movement"] == "ray"));
    assert!(pieces.iter().any(|p| p["kind"] == "exchanger" && p["movement"] == "swap" && p["range"] == 3));
}
#[test]
fn v12_contact_lock_is_a_quiescence_forcing_event() {
    let s = v12_state(vec![
        p(0, 2, Kind::King, 0),
        p(0, 1, Kind::Enchanter, 0),
        p(1, -1, Kind::Guard, 1),
    ]);
    let a = action("move", Some([0, 1]), Some([0, 0]), None);
    assert!(moves(&s).contains(&a));
    let child = apply_search(&s, &a);
    assert_eq!(rooted_count(&s, 1), 0);
    assert_eq!(rooted_count(&child, 1), 1);
    assert!(q_forcing(&s, &a, &child, false));
}
#[test]
fn frog_jumps_and_directional_rotations() {
    let s = state(vec![
        p(0, 0, Kind::Frog, 0),
        p(1, 0, Kind::Guard, 0),
        p(-1, 0, Kind::Scout, 0),
    ]);
    assert!(moves(&s).contains(&action("move", Some([0, 0]), Some([2, 0]), None)));
    assert_eq!(moves(&s).iter().filter(|a| a.kind == "rotate").count(), 5);
}
#[test]
fn repetition_is_state_based_not_ply_cutoff() {
    let mut s = state(vec![]);
    s.ply = 1000;
    assert_eq!(outcome(&s).status, "ongoing");
    let k = key(&s);
    s.history = vec![k.clone(), k];
    assert_eq!(outcome(&s).reason, "threefold_repetition");
    s.turn = 1;
    assert_eq!(outcome(&s).status, "ongoing");
}
#[test]
fn illegal_and_invalid_rejected() {
    let s = state(vec![]);
    assert!(process(
        json!({"op":"apply","state":s,"action":action("move",Some([0,3]),Some([0,-3]),None)})
    )
    .is_err());
    let mut s = state(vec![]);
    s.board.push(p(0, 3, Kind::Ghost, 1));
    assert!(validate(&mut s).is_err());
}
#[test]
fn search_finds_king_and_reports_budget() {
    let s = state(vec![p(1, -3, Kind::Guard, 0)]);
    let x = process(
        json!({"op":"search","state":s,"depth":2,"policy":"material","node_budget":10000,"seed":3}),
    )
    .unwrap();
    assert_eq!(x["value"], WIN - 1);
    assert_eq!(x["completed_depth"], 2);
    let x = process(json!({"op":"search","state":s,"depth":3,"node_budget":1})).unwrap();
    assert_eq!(x["completed_depth"], 0);
    assert_eq!(x["nodes"], 1);
    assert_eq!(x["budget_exhausted"], true);
}

fn brute(s: &State, d: u32, policy: &str) -> i32 {
    let o = outcome(s);
    if o.status != "ongoing" {
        return match o.winner {
            Some(w) if w == s.turn => WIN,
            Some(_) => -WIN,
            None => 0,
        };
    }
    if d == 0 {
        return evaluation(s, policy);
    }
    moves(s)
        .iter()
        .map(|a| -brute(&apply_unchecked(s, a), d - 1, policy))
        .max()
        .unwrap()
}
#[test]
fn alpha_beta_equals_exhaustive_minimax() {
    let states = [
        state(vec![p(0, 1, Kind::Guard, 0), p(0, -1, Kind::Ghost, 1)]),
        state(vec![p(-1, 1, Kind::Ram, 0), p(1, -1, Kind::Revenant, 1)]),
    ];
    for s in states {
        for policy in ["material", "positional", "tactical"] {
            for depth in 1..=3 {
                let expected = brute(&s, depth, policy);
                let x=process(json!({"op":"search","state":s,"depth":depth,"policy":policy,"node_budget":1000000,"seed":77})).unwrap();
                assert_eq!(x["completed_depth"], depth);
                assert_eq!(x["value"], expected);
                let a: Action = serde_json::from_value(x["action"].clone()).unwrap();
                assert_eq!(
                    -brute(&apply_unchecked(&s, &a), depth - 1, policy),
                    expected
                );
            }
        }
    }
}
#[test]
fn canonical_replay_and_reserve_conservation() {
    let original = state(vec![
        p(-2, 1, Kind::Ram, 0),
        p(-1, 2, Kind::Warper, 0),
        p(1, 1, Kind::Revenant, 0),
        p(2, -1, Kind::Ghost, 1),
        p(1, -2, Kind::Revenant, 1),
        p(-1, -1, Kind::Frog, 1),
    ]);
    let mut reversed = original.clone();
    reversed.board.reverse();
    assert_eq!(key(&original), key(&reversed));
    let mut s = original;
    let mut tape = vec![];
    for ply in 0..80 {
        if outcome(&s).status != "ongoing" {
            break;
        }
        let a = moves(&s);
        let chosen = a[(tie(332, ply) as usize) % a.len()].clone();
        let n = apply_unchecked(&s, &chosen);
        assert_eq!(n.history.last(), Some(&key(&s)));
        for side in 0..2 {
            let count = n
                .board
                .iter()
                .filter(|p| p.kind == Kind::Revenant && p.side == side)
                .count() as u32
                + n.reserves[side as usize];
            assert_eq!(count, 1);
        }
        let mut decoded = get_state(&serde_json::to_value(&n).unwrap()).unwrap();
        validate(&mut decoded).unwrap();
        assert_eq!(key(&decoded), key(&n));
        tape.push(chosen);
        s = n;
    }
    for a in tape {
        reversed = apply_unchecked(&reversed, &a);
    }
    assert_eq!(key(&s), key(&reversed));
    assert_eq!(s.history, reversed.history);
}
#[test]
fn absent_kings_are_terminal() {
    let mut s = state(vec![]);
    s.board.clear();
    assert_eq!(outcome(&s).reason, "both_kings_lost");
    // Missing own king is a loss, not stalemate.
    s.board.push(p(0, 0, Kind::King, 1));
    assert_eq!(outcome(&s).winner, Some(1));
}

#[test]
fn deeper_search_rejects_material_gain_that_loses_king() {
    // Discovered by bounded deterministic position probe (seed887). A guard can
    // take the distant queen, but only the king taking the nearby queen survives.
    let mut s = state(vec![]);
    s.radius = 2;
    s.board = vec![
        p(2, -1, Kind::King, 0),
        p(-1, 2, Kind::King, 1),
        p(0, -1, Kind::Guard, 0),
        p(-1, -1, Kind::Queen, 1),
        p(0, -2, Kind::Guard, 0),
        p(2, 0, Kind::Queen, 1),
    ];
    validate(&mut s).unwrap();
    let shallow = process(json!({"op":"search","state":s,"depth":1,"policy":"material","seed":1,"node_budget":100000})).unwrap();
    let deep = process(json!({"op":"search","state":s,"depth":2,"policy":"material","seed":1,"node_budget":100000})).unwrap();
    let a: Action = serde_json::from_value(shallow["action"].clone()).unwrap();
    assert_eq!(brute(&apply_unchecked(&s, &a), 1, "material"), WIN);
    let b: Action = serde_json::from_value(deep["action"].clone()).unwrap();
    assert!(brute(&apply_unchecked(&s, &b), 1, "material") < WIN);
    assert_eq!(b.from, Some([2, -1]));
    assert_eq!(b.to, Some([2, 0]));
}

#[test]
fn tactical_color_rotation_symmetry_and_finite_values() {
    let s = state(vec![
        p(0, 1, Kind::Lancer, 0),
        p(-2, 1, Kind::Ram, 0),
        p(-1, 2, Kind::Warper, 0),
        p(1, -1, Kind::Ghost, 1),
        p(-1, -1, Kind::Revenant, 1),
        p(2, -1, Kind::Frog, 1),
    ]);
    let mut rotated = s.clone();
    for p in &mut rotated.board {
        p.q = -p.q;
        p.r = -p.r;
        p.side = 1 - p.side;
        p.facing = (p.facing + 3) % 6;
    }
    rotated.turn = 1 - s.turn;
    rotated.reserves.swap(0, 1);
    canon(&mut rotated);
    assert_eq!(evaluation(&s, "tactical"), evaluation(&rotated, "tactical"));
    rotated.turn = 1 - rotated.turn;
    assert_eq!(
        evaluation(&s, "tactical"),
        -evaluation(&rotated, "tactical")
    );
    assert!(evaluation(&s, "tactical").abs() < WIN);
    let response=process(json!({"op":"search","state":s,"depth":2,"policy":"tactical","seed":1,"node_budget":100000})).unwrap();
    assert_eq!(response["completed_depth"], 2);
    assert!(response["value"].as_i64().is_some());
}
