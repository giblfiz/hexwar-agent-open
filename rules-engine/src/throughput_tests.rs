use super::*;

#[test]
fn timed_search_discards_only_history_and_public_apply_retains_it() {
    for version in ["hexwar-lab-v1", "hexwar-lab-v2", "hexwar-lab-v3", "hexwar-lab-v4"] {
        let mut s = State {
            rules_version: version.into(), radius: 3, turn: 0, reserves: [1, 1],
            ply: 50, history: vec!["old replay entry".repeat(100); 50],
            board: vec![
                Piece {q:0,r:3,kind:Kind::King,side:0,facing:0},
                Piece {q:0,r:-3,kind:Kind::King,side:1,facing:3},
                Piece {q:0,r:0,kind:Kind::Ram,side:0,facing:0},
                Piece {q:1,r:0,kind:Kind::Revenant,side:0,facing:0},
                Piece {q:-1,r:0,kind:Kind::Warper,side:0,facing:0},
                Piece {q:1,r:-1,kind:Kind::Ghost,side:1,facing:3},
            ],
        };
        canon(&mut s);
        for a in moves(&s) {
            let public = apply_unchecked(&s, &a);
            let mut private = apply_search(&s, &a);
            assert_eq!(public.history.len(), 51);
            assert_eq!(public.history.last().unwrap(), &key(&s));
            if timed(&s) { assert!(private.history.is_empty()); }
            else { assert_eq!(private.history, public.history); }
            assert_eq!(terminal(&private).status, terminal(&public).status);
            assert_eq!(moves(&private), moves(&public));
            assert_eq!(evaluation(&private, "tactical"), evaluation(&public, "tactical"));
            private.history = public.history.clone();
            assert_eq!(serde_json::to_value(private).unwrap(), serde_json::to_value(public).unwrap());
        }
        let active: Vec<_> = moves(&s).into_iter().filter(|a| a.kind != "rotate" && a.kind != "rebirth").collect();
        assert_eq!(active, moves_for_activity(&s, false));
    }
}

#[test]
fn action_names_are_borrowed_but_json_remains_compatible() {
    for k in ["move", "rotate", "push", "swap", "rebirth"] {
        let a = action(k, Some([0,0]), Some([0,1]), None);
        assert!(matches!(a.kind, Cow::Borrowed(_)));
        let parsed: Action = serde_json::from_value(serde_json::to_value(&a).unwrap()).unwrap();
        assert_eq!(a, parsed);
    }
}

#[test]
fn occupancy_matches_reference_on_every_supported_board_and_boundary() {
    for radius in 2..=5 {
        let mut s = State { rules_version: "hexwar-lab-v4".into(), radius,
            board: vec![], turn: 0, reserves: [0,0], ply: 0, history: vec![] };
        for q in -radius..=radius {
            for r in -radius..=radius {
                if distance(q,r) <= radius && (q-r) % 3 == 0 {
                    s.board.push(Piece {q,r,kind:Kind::Guard,side:0,facing:0});
                }
            }
        }
        for reverse in [false, true] {
            if reverse { s.board.reverse(); }
            let occupancy = Occupancy::new(&s);
            for q in -6..=6 { for r in -6..=6 {
                assert_eq!(occupancy.at([q,r]), at(&s,[q,r]));
            }}
        }
    }
}
