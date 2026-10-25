use super::*;
use std::collections::BTreeSet;

fn board(kind: Kind, facing: u8) -> State {
    State { rules_version: "hexwar-lab-v5".into(), radius: 5, turn: 0,
        reserves: [0,0], ply: 0, history: vec![],
        board: vec![Piece { q:0,r:0,kind,side:0,facing }] }
}

// Independent old-catalogue specification: relative directions and ranges.
fn pattern(k: Kind) -> (&'static [usize], i32) {
    match k {
        Kind::Pawn => (&[0],1), Kind::Crab => (&[1,3,5],1), Kind::Flanker => (&[1,5],1),
        Kind::Strider => (&[0],2), Kind::Dancer => (&[1,5],2),
        Kind::Hound => (&[0,1,5],2), Kind::Triton => (&[1,3,5],2),
        Kind::Dragoon => (&[0,1,5],3), Kind::Courser => (&[0,1,2,3,4,5],3),
        Kind::Pike => (&[0],10), Kind::Rook => (&[0,3],10),
        Kind::Bishop => (&[1,2,4,5],10), Kind::Chariot => (&[0,1,5],10),
        Kind::Triskelion => (&[1,3,5],10), _ => unreachable!(),
    }
}

#[test]
fn restored_rays_match_catalogue_all_facings_and_blockers() {
    for &kind in &KINDS[11..25] {
        let (dirs,range) = pattern(kind);
        for facing in 0..6 {
            let mut s = board(kind, facing);
            for friendly in [None, Some(false), Some(true)] {
                s.board.truncate(1);
                if let Some(ally) = friendly {
                    let v=DIRS[facing as usize];
                    s.board.push(Piece{q:v[0],r:v[1],kind:Kind::Guard,side:if ally {0}else{1},facing:0});
                }
                let mut expected=BTreeSet::new();
                for &relative in dirs {
                    let v=DIRS[(relative+facing as usize)%6];
                    for n in 1..=range.min(5) {
                        if relative==0 && friendly.is_some() {
                            if n==1 && friendly==Some(false) {expected.insert([v[0],v[1]]);}
                            break;
                        }
                        expected.insert([n*v[0],n*v[1]]);
                    }
                }
                let actual:BTreeSet<_>=moves(&s).iter().filter(|a|a.from==Some([0,0]) && a.kind=="move").map(|a|a.to.unwrap()).collect();
                assert_eq!(actual,expected,"{kind:?} facing {facing} ally {friendly:?}");
                for q in -5..=5 { for r in -5..=5 {
                    if distance(q,r)>5 || (friendly==Some(true) && [q,r]==DIRS[facing as usize]) {continue;}
                    assert_eq!(can_capture(&s,&s.board[0],[q,r]),expected.contains(&[q,r]),"capture {kind:?} {q},{r}");
                }}
                let rotations=moves(&s).iter().filter(|a|a.from==Some([0,0]) && a.kind=="rotate").count();
                assert_eq!(rotations,if kind==Kind::Courser {0}else{5});
            }
        }
    }
}

#[test]
fn forward_jump_arcs_match_original_angle_formula() {
    for (kind,range,count) in [(Kind::Knight,2,5),(Kind::Locust,3,7),(Kind::Cricket,3,18)] {
        for facing in 0..6 {
            let mut s=board(kind,facing);
            // All neighboring squares occupied: jump must ignore intervening bodies.
            for v in DIRS {s.board.push(Piece{q:v[0],r:v[1],kind:Kind::Guard,side:0,facing:0});}
            let mut expected=BTreeSet::new();
            for q in -range..=range {for r in -range..=range {
                if distance(q,r)!=range {continue;}
                let angle=(0.8660254*q as f64+1.7320508*r as f64).atan2(1.5*q as f64).to_degrees().rem_euclid(360.0);
                let forward=[270.0,330.0,30.0,90.0,150.0,210.0][facing as usize];
                let diff=((angle-forward+180.0).rem_euclid(360.0)-180.0).abs();
                if kind==Kind::Cricket || diff<=75.0 {expected.insert([q,r]);}
            }}
            assert_eq!(expected.len(),count);
            let actual:BTreeSet<_>=moves(&s).iter().filter(|a|a.from==Some([0,0]) && a.kind=="move").map(|a|a.to.unwrap()).collect();
            assert_eq!(actual,expected,"{kind:?} facing {facing}");
            for q in -3..=3 {for r in -3..=3 {assert_eq!(can_capture(&s,&s.board[0],[q,r]),expected.contains(&[q,r]));}}
            let target=*expected.iter().next().unwrap();
            s.board.push(Piece{q:target[0],r:target[1],kind:Kind::Guard,side:0,facing:0});
            assert!(!moves(&s).iter().any(|a|a.from==Some([0,0]) && a.to==Some(target)));
            s.board.last_mut().unwrap().side=1;
            assert!(moves(&s).iter().any(|a|a.from==Some([0,0]) && a.to==Some(target)));
        }
    }
}

#[test]
fn catalogue_version_gates_and_material_roundtrip() {
    for &kind in &KINDS[11..28] {
        let mut s=board(kind,0);
        assert!(validate(&mut s).is_ok());
        for version in 1..=4 {s.rules_version=format!("hexwar-lab-v{version}");assert!(validate(&mut s).is_err());}
    }
    for kind in [Kind::CrownedLancer, Kind::CharmedStrider] {
        let mut s=board(kind,0);
        assert!(validate(&mut s).is_err());
        s.rules_version="hexwar-lab-v10".into();
        assert!(validate(&mut s).is_ok());
    }
    assert_eq!(serde_json::from_value::<Kind>(json!("ranger")).unwrap(),Kind::Cleaver);
    let values=MaterialValues::default();
    let roundtrip=MaterialValues::parse(&json!({"material_values":values.json()})).unwrap();
    for k in KINDS {assert_eq!(values.get(k),roundtrip.get(k));}
    for (i,k) in KINDS.iter().enumerate(){assert_eq!(*k as usize,i);}
}

#[test]
fn old_piece_legal_actions_unchanged_in_v5() {
    for &kind in &KINDS[..11] {for facing in 0..6 {
        let mut s=board(kind,facing);
        let current=moves(&s);
        s.rules_version="hexwar-lab-v4".into();
        assert_eq!(current,moves(&s));
    }}
}

#[test]
fn stateless_catalog_and_legacy_material_manifest() {
    let c=process(json!({"op":"catalog"})).unwrap();
    assert_eq!(c["pieces"].as_array().unwrap().len(),28);
    assert_eq!(c["aliases"]["ranger"],"cleaver");
    let mut s=board(Kind::Cleaver,0);
    for version in ["hexwar-lab-v4","hexwar-lab-v5"] {
        s.rules_version=version.into();
        let response=process(json!({"op":"evaluate","state":s})).unwrap();
        assert_eq!(response["effective_config"]["material_values"].as_object().unwrap().len(),if version.ends_with('5'){27}else{10});
    }
}

#[test]
fn v5_keeps_friendly_ram_and_timed_terminal() {
    let mut s=board(Kind::Ram,0);
    s.board.extend([
        Piece{q:0,r:-1,kind:Kind::King,side:0,facing:0},
        Piece{q:4,r:0,kind:Kind::King,side:1,facing:0},
    ]);
    assert!(moves(&s).iter().any(|a|a.kind=="push" && a.to==Some([0,-1])));
    s.ply=100;
    assert_eq!(terminal(&s).winner,Some(0));
}
