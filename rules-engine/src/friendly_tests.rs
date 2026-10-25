use super::*;

fn piece(q: i32, r: i32, kind: Kind, side: u8, facing: u8) -> Piece {
    Piece {
        q,
        r,
        kind,
        side,
        facing,
    }
}
fn state(extra: Vec<Piece>) -> State {
    let mut s = State {
        rules_version: "hexwar-lab-v3".into(),
        radius: 3,
        board: vec![
            piece(0, 3, Kind::King, 0, 0),
            piece(-2, -1, Kind::King, 1, 3),
        ],
        turn: 0,
        reserves: [0, 0],
        ply: 0,
        history: vec![],
    };
    s.board.retain(|p| {
        !extra
            .iter()
            .any(|e| e.kind == Kind::King && e.side == p.side)
    });
    s.board.extend(extra);
    validate(&mut s).unwrap();
    s
}

#[test]
fn friendly_push_delivery_preserves_facing_and_old_rules() {
    for kind in [Kind::Lancer, Kind::Ram, Kind::Ghost, Kind::King] {
        let mut s = state(vec![piece(-2, 0, Kind::Ram, 0, 4), piece(0, 0, kind, 0, 2)]);
        if kind == Kind::King {
            s.board.retain(|p| p.r != 3);
        }
        let a = action("push", Some([-2, 0]), Some([0, 0]), None);
        assert!(moves(&s).contains(&a));
        let n = apply_unchecked(&s, &a);
        assert_eq!(n.board.len(), s.board.len());
        assert_eq!(n.board[at(&n, [1, 0]).unwrap()].kind, kind);
        assert_eq!(n.board[at(&n, [1, 0]).unwrap()].facing, 2);
        assert_eq!(n.board[at(&n, [0, 0]).unwrap()].facing, 4);
        assert_eq!(n.ply, 1);
        assert_eq!(n.turn, 1);
        for version in ["hexwar-lab-v1", "hexwar-lab-v2"] {
            s.rules_version = version.into();
            assert!(!moves(&s).contains(&a));
            assert!(process(json!({"op":"apply","state":s,"action":a})).is_err());
        }
    }
}

#[test]
fn friendly_push_cannot_chain_or_leave_board() {
    for blocker in [
        piece(1, 0, Kind::Guard, 0, 0),
        piece(1, 0, Kind::Guard, 1, 3),
    ] {
        let s = state(vec![
            piece(-2, 0, Kind::Ram, 0, 0),
            piece(0, 0, Kind::Guard, 0, 0),
            blocker,
        ]);
        assert!(!moves(&s).contains(&action("push", Some([-2, 0]), Some([0, 0]), None)));
    }
    let s = state(vec![
        piece(0, 0, Kind::Ram, 0, 0),
        piece(3, 0, Kind::Ram, 0, 0),
    ]);
    assert!(!moves(&s).contains(&action("push", Some([0, 0]), Some([3, 0]), None)));
}

#[test]
fn friendly_delivery_creates_forcing_material_and_king_threats() {
    for kind in [Kind::Guard, Kind::King] {
        let mut s = state(vec![
            piece(-2, 0, Kind::Ram, 0, 0),
            piece(0, 0, Kind::Lancer, 0, 0),
            piece(1, -2, kind, 1, 3),
        ]);
        if kind == Kind::King {
            s.board.retain(|p| p.r != -3);
        }
        let a = action("push", Some([-2, 0]), Some([0, 0]), None);
        let n = apply_unchecked(&s, &a);
        assert!(!king_attacked(&s, 1));
        assert!(q_forcing(&s, &a, &n, false));
    }
}

#[test]
fn v3_clock_identity_urgency_and_deadline() {
    let mut s = state(vec![piece(1, 0, Kind::Guard, 0, 0)]);
    let k = key(&s);
    s.ply = 50;
    assert_ne!(key(&s), k);
    let mut old = s.clone();
    old.rules_version = "hexwar-lab-v2".into();
    assert_ne!(key(&s), key(&old));
    let answer = process(json!({"op":"evaluate","state":s})).unwrap();
    assert_eq!(answer["value"], 75);
    s.ply = 100;
    assert_eq!(outcome(&s).reason, "deadline_equal");
    s.ply = 101;
    assert!(validate(&mut s).is_err());
}

#[test]
fn material_override_validation_and_defaults() {
    let s = state(vec![piece(1, 0, Kind::Ram, 0, 0)]);
    for bad in [
        json!({"king":1}),
        json!({"dragon":2}),
        json!({"ram":-1}),
        json!({"ram":2001}),
        json!({"ram":1.5}),
        json!({"ram":true}),
        json!(null),
        json!([]),
    ] {
        assert!(process(json!({"op":"evaluate","state":s,"material_values":bad})).is_err());
    }
    for cost in [0, 17, 2000] {
        let answer =
            process(json!({"op":"evaluate","state":s,"material_values":{"ram":cost}})).unwrap();
        assert_eq!(answer["value"], cost);
        assert_eq!(answer["material_values"]["ram"], cost);
        assert_eq!(answer["material_values"]["guard"], 100);
    }
    let default = process(json!({"op":"search","state":s,"depth":2,"node_budget":100000})).unwrap();
    let explicit = process(
        json!({"op":"search","state":s,"depth":2,"node_budget":100000,"material_values":{}}),
    )
    .unwrap();
    assert_eq!(default, explicit);
}

#[test]
fn custom_values_change_search_choice_and_reserve_value() {
    let s = state(vec![
        piece(0, 0, Kind::Queen, 0, 0),
        piece(1, 0, Kind::Guard, 1, 3),
        piece(-1, 0, Kind::Scout, 1, 3),
    ]);
    for (values, target) in [
        (json!({"guard":500,"scout":1}), [1, 0]),
        (json!({"guard":1,"scout":500}), [-1, 0]),
    ] {
        let response = process(
            json!({"op":"search","state":s,"depth":1,"policy":"material","material_values":values}),
        )
        .unwrap();
        let a: Action = serde_json::from_value(response["action"].clone()).unwrap();
        assert_eq!(a.to, Some(target));
    }
    let mut s = state(vec![]);
    s.reserves = [2, 0];
    let response =
        process(json!({"op":"evaluate","state":s,"material_values":{"revenant":800}})).unwrap();
    assert_eq!(response["value"], 600);
}
