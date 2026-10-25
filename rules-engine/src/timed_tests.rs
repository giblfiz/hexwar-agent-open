use super::*;

fn p(q: i32, r: i32, kind: Kind, side: u8, facing: u8) -> Piece {
    Piece {
        q,
        r,
        kind,
        side,
        facing,
    }
}
fn s(ply: u64, board: Vec<Piece>) -> State {
    let mut s = State {
        rules_version: "hexwar-lab-v2".into(),
        radius: 2,
        board,
        turn: 0,
        reserves: [0, 0],
        ply,
        history: vec![],
    };
    validate(&mut s).unwrap();
    s
}
fn searcher(urgency: bool, prefer: bool, q: bool, qd: u32) -> Search {
    Search {
        material_values: MaterialValues::default(),
        nodes: 0,
        budget: 1000000,
        policy: "tactical".into(),
        urgency,
        prefer_short_wins: prefer,
        quiescence: q,
        q_depth: qd,
        q_nodes: 0,
        q_cap_hits: 0,
        max_q_ply: 0,
    }
}
// Exhaustive reference has no alpha-beta, ordering, or quiescence filtering.
fn exact(s: &State, depth: u32, root_ply: u32, urgency: bool, prefer: bool) -> i32 {
    let t = outcome(s);
    if t.status != "ongoing" {
        let mag = WIN - if prefer { root_ply as i32 } else { 0 };
        return match t.winner {
            Some(w) if w == s.turn => mag,
            Some(_) => -mag,
            None => 0,
        };
    }
    if depth == 0 {
        return search_evaluation(s, "tactical", urgency);
    }
    moves(s)
        .iter()
        .map(|a| {
            -exact(
                &apply_unchecked(s, a),
                depth - 1,
                root_ply + 1,
                urgency,
                prefer,
            )
        })
        .max()
        .unwrap()
}

#[test]
fn deadline_and_king_loss_precedence() {
    let state = s(
        99,
        vec![p(0, 0, Kind::King, 0, 0), p(1, 0, Kind::King, 1, 3)],
    );
    let capture = action("move", Some([0, 0]), Some([1, 0]), None);
    let n = apply_unchecked(&state, &capture);
    assert_eq!(n.ply, 100);
    assert_eq!(outcome(&n).reason, "king_lost");
    assert_eq!(outcome(&n).winner, Some(0));
    let mut state = s(
        100,
        vec![p(0, 0, Kind::King, 0, 0), p(0, -2, Kind::King, 1, 3)],
    );
    assert_eq!(outcome(&state).reason, "deadline_proximity");
    assert_eq!(outcome(&state).winner, Some(0));
    let king_index = at(&state, [0, 0]).unwrap();
    state.board[king_index].r = 2;
    assert_eq!(outcome(&state).reason, "deadline_equal");
    assert!(process(
        json!({"op":"apply","state":state,"action":action("move",Some([0,2]),Some([0,1]),None)})
    )
    .is_err());
    state.ply = 101;
    assert!(validate(&mut state).is_err());
}

#[test]
fn final_action_ghost_suicide_is_loss_not_center_win() {
    let state = s(
        99,
        vec![
            p(1, 0, Kind::King, 0, 0),
            p(0, 0, Kind::Ghost, 1, 3),
            p(0, -2, Kind::King, 1, 3),
        ],
    );
    let n = apply_unchecked(&state, &action("move", Some([1, 0]), Some([0, 0]), None));
    assert_eq!(outcome(&n).reason, "king_lost");
    assert_eq!(outcome(&n).winner, Some(1));
}

#[test]
fn clock_identity_and_v1_repetition_are_separate() {
    let mut state = s(
        20,
        vec![p(0, 2, Kind::King, 0, 0), p(0, -2, Kind::King, 1, 3)],
    );
    let k = key(&state);
    state.history = vec![k.clone(), k.clone()];
    assert_eq!(outcome(&state).status, "ongoing");
    state.ply += 1;
    assert_ne!(k, key(&state));
    state.rules_version = "hexwar-lab-v1".into();
    let k = key(&state);
    state.ply += 1;
    assert_eq!(k, key(&state));
    state.history = vec![k.clone(), k];
    assert_eq!(outcome(&state).reason, "threefold_repetition");
}

#[test]
fn timed_alpha_beta_matches_exhaustive_reference() {
    for ply in [0, 97, 98, 99] {
        for urgency in [false, true] {
            for prefer in [false, true] {
                let state = s(
                    ply,
                    vec![
                        p(0, 2, Kind::King, 0, 0),
                        p(0, -2, Kind::King, 1, 3),
                        p(-1, 1, Kind::Guard, 0, 0),
                        p(1, -1, Kind::Scout, 1, 3),
                    ],
                );
                for depth in 1..=3 {
                    let mut engine = searcher(urgency, prefer, false, 0);
                    assert_eq!(
                        engine.visit(&state, depth, -WIN - 1, WIN + 1, 0).unwrap(),
                        exact(&state, depth, 0, urgency, prefer),
                        "ply{ply} depth{depth} urgency{urgency} prefer{prefer}"
                    );
                }
            }
        }
    }
}

#[test]
fn mate_preference_takes_immediate_win_for_every_seed() {
    let mut state = s(
        20,
        vec![
            p(-1, 0, Kind::King, 1, 3),
            p(0, 1, Kind::King, 0, 0),
            p(1, 0, Kind::Guard, 1, 3),
            p(1, 1, Kind::Lancer, 1, 3),
        ],
    );
    state.turn = 1;
    state.rules_version = "hexwar-lab-v1".into();
    for seed in 0..12 {
        let answer=process(json!({"op":"search","state":state,"depth":4,"policy":"tactical","seed":seed,"node_budget":1000000})).unwrap();
        assert_eq!(answer["value"], WIN - 1);
        let a: Action = serde_json::from_value(answer["action"].clone()).unwrap();
        assert_eq!(outcome(&apply_unchecked(&state, &a)).winner, Some(1));
    }
}

#[test]
fn mate_preference_delays_forced_loss_instead_of_ghost_suicide() {
    let state = s(
        97,
        vec![
            p(2, 0, Kind::King, 0, 0),
            p(0, 0, Kind::King, 1, 3),
            p(1, 0, Kind::Ghost, 1, 3),
        ],
    );
    let answer=process(json!({"op":"search","state":state,"depth":3,"policy":"tactical","seed":7,"node_budget":1000000})).unwrap();
    // Suicide loses on this action; either escape is captured by the Ghost on
    // the following action. Prefer the latter, without inventing a draw.
    assert_eq!(answer["value"], -WIN + 2);
    let a: Action = serde_json::from_value(answer["action"].clone()).unwrap();
    assert_ne!(a.to, Some([1, 0]));
    assert_eq!(exact(&state, 3, 0, true, true), -WIN + 2);
}

#[test]
fn q_threat_responses_cannot_stand_pat_or_safely_take_ghost() {
    let mut board = vec![p(0, 0, Kind::King, 0, 0), p(0, -2, Kind::King, 1, 3)];
    board.extend(DIRS.iter().map(|v| p(v[0], v[1], Kind::Ghost, 1, 3)));
    let state = s(0, board);
    assert!(king_attacked(&state, 0));
    let mut engine = searcher(false, true, true, 1);
    assert_eq!(
        engine.qvisit(&state, 1, -WIN - 1, WIN + 1, 0, 0).unwrap(),
        -WIN + 1
    );
    let mut capped = searcher(false, true, true, 0);
    let value = capped.qvisit(&state, 0, -WIN - 1, WIN + 1, 0, 0).unwrap();
    assert!(value.abs() < WIN / 2);
    assert_eq!(capped.q_cap_hits, 1);
}

#[test]
fn q_attacked_king_can_countercapture_enemy_king() {
    let state = s(
        0,
        vec![
            p(0, 0, Kind::King, 0, 0),
            p(1, 0, Kind::Ghost, 1, 3),
            p(-2, 1, Kind::King, 1, 3),
            p(-2, 0, Kind::Queen, 0, 0),
        ],
    );
    assert!(king_attacked(&state, 0));
    let mut engine = searcher(false, true, true, 1);
    assert_eq!(
        engine.qvisit(&state, 1, -WIN - 1, WIN + 1, 0, 0).unwrap(),
        WIN - 1
    );
}

#[test]
fn q_classifies_king_threat_rotations_and_allied_ram_attacks() {
    let mut state = s(
        0,
        vec![
            p(0, 2, Kind::King, 0, 0),
            p(0, -2, Kind::King, 1, 3),
            p(0, 0, Kind::Lancer, 0, 2),
        ],
    );
    let a = action("rotate", Some([0, 0]), None, Some(0));
    let child = apply_unchecked(&state, &a);
    assert!(!king_attacked(&state, 1));
    assert!(q_forcing(&state, &a, &child, false));
    state.board = vec![
        p(0, 2, Kind::King, 0, 0),
        p(0, -2, Kind::King, 1, 3),
        p(-2, 0, Kind::Ram, 0, 0),
        p(0, 0, Kind::Guard, 1, 3),
        p(1, -2, Kind::Lancer, 0, 3),
    ];
    let a = action("push", Some([-2, 0]), Some([0, 0]), None);
    assert!(moves(&state).contains(&a));
    let child = apply_unchecked(&state, &a);
    assert!(q_forcing(&state, &a, &child, false));
}

#[test]
fn q_deadline_window_is_full_width_without_pretend_pass() {
    // Compare every nonadjacent bare-king pair at one action to deadline. These
    // include positions where static evaluation/standing still disagrees with
    // the mandatory clock-advancing legal move.
    let cells: Vec<_> = (-2..=2)
        .flat_map(|q| {
            (-2..=2).filter_map(move |r| {
                if distance(q, r) <= 2 {
                    Some((q, r))
                } else {
                    None
                }
            })
        })
        .collect();
    for &(wq, wr) in &cells {
        for &(bq, br) in &cells {
            if distance(wq - bq, wr - br) < 2 {
                continue;
            }
            let state = s(
                99,
                vec![p(wq, wr, Kind::King, 0, 0), p(bq, br, Kind::King, 1, 3)],
            );
            let mut engine = searcher(true, true, true, 1);
            assert_eq!(
                engine.qvisit(&state, 1, -WIN - 1, WIN + 1, 0, 0).unwrap(),
                exact(&state, 1, 0, true, true)
            );
        }
    }
}

#[test]
fn q_budget_caps_and_incomplete_iteration_are_transparent() {
    let state = s(
        20,
        vec![
            p(0, 2, Kind::King, 0, 0),
            p(0, -2, Kind::King, 1, 3),
            p(0, 0, Kind::Queen, 0, 0),
            p(1, -1, Kind::Ghost, 1, 3),
        ],
    );
    for cap in [0, 1, 2] {
        let answer=process(json!({"op":"search","state":state,"depth":1,"policy":"tactical","quiescence":true,"q_depth":cap,"node_budget":1000000})).unwrap();
        assert_eq!(answer["completed_depth"], 1);
        assert!(answer["max_q_ply"].as_u64().unwrap() <= cap);
        assert!(answer["q_nodes"].as_u64().unwrap() <= answer["nodes"].as_u64().unwrap());
    }
    let answer=process(json!({"op":"search","state":state,"depth":5,"quiescence":true,"q_depth":4,"node_budget":1})).unwrap();
    assert_eq!(answer["nodes"], 1);
    assert_eq!(answer["q_nodes"], 1);
    assert_eq!(answer["completed_depth"], 0);
    assert_eq!(answer["budget_exhausted"], true);
    for field in ["quiescence", "urgency", "prefer_short_wins"] {
        let mut request = json!({"op":"search","state":state,"depth":1});
        request[field] = json!("yes");
        assert!(process(request).is_err());
    }
    assert!(process(json!({"op":"search","state":state,"depth":1,"q_depth":9})).is_err());

    let first=process(json!({"op":"search","state":state,"depth":1,"quiescence":true,"q_depth":2,"node_budget":1000000,"seed":99})).unwrap();
    let budget = first["nodes"].as_u64().unwrap() + 1;
    let interrupted=process(json!({"op":"search","state":state,"depth":3,"quiescence":true,"q_depth":2,"node_budget":budget,"seed":99})).unwrap();
    assert_eq!(interrupted["completed_depth"], 1);
    assert_eq!(interrupted["action"], first["action"]);
    assert_eq!(interrupted["value"], first["value"]);
    assert_eq!(interrupted["nodes"], budget);
    assert_eq!(interrupted["budget_exhausted"], true);
}

#[test]
fn q_root_extension_solves_the_whole_remaining_clock() {
    for pieces in [
        vec![p(0, 2, Kind::King, 0, 0), p(0, -2, Kind::King, 1, 3)],
        vec![
            p(0, 2, Kind::King, 0, 0),
            p(0, -2, Kind::King, 1, 3),
            p(-1, 1, Kind::Ram, 0, 0),
            p(1, -1, Kind::Lancer, 1, 3),
        ],
    ] {
        let state = s(97, pieces);
        let answer=process(json!({"op":"search","state":state,"depth":1,"policy":"tactical","quiescence":true,"q_depth":2,"node_budget":1000000})).unwrap();
        assert_eq!(answer["completed_depth"], 1);
        assert_eq!(answer["value"], exact(&state, 3, 0, true, true));
        assert_eq!(answer["q_cap_hits"], 0);
    }
}

#[test]
fn urgency_tapers_material_smoothly_and_is_color_symmetric() {
    let mut state = s(
        0,
        vec![
            p(0, 1, Kind::King, 0, 0),
            p(0, -2, Kind::King, 1, 3),
            p(2, -2, Kind::Queen, 1, 3),
        ],
    );
    let early = search_evaluation(&state, "material", true);
    assert_eq!(early, -550);
    state.ply = 50;
    let middle = search_evaluation(&state, "material", true);
    assert_eq!(middle, (-550 * 7500 + 600 * 2500) / 10000);
    state.ply = 99;
    let late = search_evaluation(&state, "material", true);
    assert!(late > middle && middle > early);
    let mut reflected = state.clone();
    for p in &mut reflected.board {
        p.q = -p.q;
        p.r = -p.r;
        p.side = 1 - p.side;
        p.facing = (p.facing + 3) % 6;
    }
    reflected.turn = 1;
    assert_eq!(late, search_evaluation(&reflected, "material", true));
}
