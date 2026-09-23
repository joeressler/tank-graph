//! Interchange records and graph types defined in Milestone 2.
//!
//! Graph enums are declared so later milestones can use them as `HashMap` keys.
//! This milestone does not construct a graph.

use serde::de::{self, Deserialize, Deserializer, Error, IgnoredAny, MapAccess, Visitor};
use std::fmt;
use std::hash::Hash;

/// Deserialization-only interchange record matching the JSON Schema fields.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TankRecord {
    pub name: String,
    pub display_name: String,
    pub class_name: String,
    pub subclasses: Vec<String>,
    pub nation: String,
    pub tier: u8,
    pub strategies: Vec<String>,
    pub keywords: Vec<String>,
}

/// Graph node variants. Equality is variant-sensitive.
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub enum NodeType {
    Tank {
        name: String,
        display_name: String,
        nation: String,
        tier: u8,
    },
    Class(String),
    Strategy(String),
    Keyword(String),
}

/// Directed edge kinds used beginning in Milestone 3.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum EdgeType {
    IsSubclassOf,
    IsAClassOf,
    RecommendedTactic,
    AssociatedWithKeyword,
}

/// Directed adjacency-list graph reserved for Milestone 3 construction.
pub type TankGraph = petgraph::graph::DiGraph<NodeType, EdgeType>;

struct TankRecordFields {
    name: String,
    display_name: String,
    class_name: String,
    subclasses: Vec<String>,
    nation: String,
    tier: StrictU8,
    strategies: Vec<String>,
    keywords: Vec<String>,
}

/// Rejects JSON floats, numeric strings, and values outside `u8`.
struct StrictU8(u8);

impl<'de> Deserialize<'de> for StrictU8 {
    fn deserialize<D>(deserializer: D) -> Result<Self, D::Error>
    where
        D: Deserializer<'de>,
    {
        struct StrictU8Visitor;

        impl Visitor<'_> for StrictU8Visitor {
            type Value = StrictU8;

            fn expecting(&self, formatter: &mut fmt::Formatter) -> fmt::Result {
                formatter.write_str("an unsigned 8-bit integer")
            }

            fn visit_u64<E>(self, value: u64) -> Result<Self::Value, E>
            where
                E: de::Error,
            {
                u8::try_from(value)
                    .map(StrictU8)
                    .map_err(|_| E::custom("integer overflow"))
            }

            fn visit_i64<E>(self, value: i64) -> Result<Self::Value, E>
            where
                E: de::Error,
            {
                u8::try_from(value).map(StrictU8).map_err(|_| {
                    if value < 0 {
                        E::custom("negative number")
                    } else {
                        E::custom("integer overflow")
                    }
                })
            }

            fn visit_f64<E>(self, _value: f64) -> Result<Self::Value, E>
            where
                E: de::Error,
            {
                Err(E::custom("non-integer number"))
            }

            fn visit_str<E>(self, _value: &str) -> Result<Self::Value, E>
            where
                E: de::Error,
            {
                Err(E::custom("numeric string"))
            }

            fn visit_bool<E>(self, _value: bool) -> Result<Self::Value, E>
            where
                E: de::Error,
            {
                Err(E::custom("expected unsigned integer"))
            }
        }

        deserializer.deserialize_any(StrictU8Visitor)
    }
}

impl<'de> Deserialize<'de> for TankRecord {
    fn deserialize<D>(deserializer: D) -> Result<Self, D::Error>
    where
        D: Deserializer<'de>,
    {
        struct TankRecordVisitor;

        impl<'de> Visitor<'de> for TankRecordVisitor {
            type Value = TankRecord;

            fn expecting(&self, formatter: &mut fmt::Formatter) -> fmt::Result {
                formatter.write_str("a tank interchange object")
            }

            fn visit_map<A>(self, mut map: A) -> Result<Self::Value, A::Error>
            where
                A: MapAccess<'de>,
            {
                let mut seen = SeenFields::default();
                let mut fields = PendingFields::default();

                while let Some(key) = map.next_key::<String>()? {
                    if !seen.mark(&key) {
                        return Err(A::Error::custom(format!("duplicate field `{key}`")));
                    }
                    match key.as_str() {
                        "name" => fields.name = Some(map.next_value()?),
                        "display_name" => fields.display_name = Some(map.next_value()?),
                        "class" => fields.class_name = Some(map.next_value()?),
                        "subclasses" => fields.subclasses = Some(map.next_value()?),
                        "nation" => fields.nation = Some(map.next_value()?),
                        "tier" => fields.tier = Some(map.next_value()?),
                        "strategies" => fields.strategies = Some(map.next_value()?),
                        "keywords" => fields.keywords = Some(map.next_value()?),
                        other => {
                            let _: IgnoredAny = map.next_value()?;
                            return Err(A::Error::unknown_field(
                                other,
                                &[
                                    "name",
                                    "display_name",
                                    "class",
                                    "subclasses",
                                    "nation",
                                    "tier",
                                    "strategies",
                                    "keywords",
                                ],
                            ));
                        }
                    }
                }

                let TankRecordFields {
                    name,
                    display_name,
                    class_name,
                    subclasses,
                    nation,
                    tier,
                    strategies,
                    keywords,
                } = fields.finish()?;

                Ok(TankRecord {
                    name,
                    display_name,
                    class_name,
                    subclasses,
                    nation,
                    tier: tier.0,
                    strategies,
                    keywords,
                })
            }
        }

        deserializer.deserialize_map(TankRecordVisitor)
    }
}

#[derive(Default)]
struct SeenFields {
    name: bool,
    display_name: bool,
    class_name: bool,
    subclasses: bool,
    nation: bool,
    tier: bool,
    strategies: bool,
    keywords: bool,
}

impl SeenFields {
    fn mark(&mut self, key: &str) -> bool {
        let slot = match key {
            "name" => &mut self.name,
            "display_name" => &mut self.display_name,
            "class" => &mut self.class_name,
            "subclasses" => &mut self.subclasses,
            "nation" => &mut self.nation,
            "tier" => &mut self.tier,
            "strategies" => &mut self.strategies,
            "keywords" => &mut self.keywords,
            _ => return true,
        };
        if *slot {
            return false;
        }
        *slot = true;
        true
    }
}

#[derive(Default)]
struct PendingFields {
    name: Option<String>,
    display_name: Option<String>,
    class_name: Option<String>,
    subclasses: Option<Vec<String>>,
    nation: Option<String>,
    tier: Option<StrictU8>,
    strategies: Option<Vec<String>>,
    keywords: Option<Vec<String>>,
}

impl PendingFields {
    fn finish<E: de::Error>(self) -> Result<TankRecordFields, E> {
        Ok(TankRecordFields {
            name: self.name.ok_or_else(|| E::missing_field("name"))?,
            display_name: self
                .display_name
                .ok_or_else(|| E::missing_field("display_name"))?,
            class_name: self.class_name.ok_or_else(|| E::missing_field("class"))?,
            subclasses: self
                .subclasses
                .ok_or_else(|| E::missing_field("subclasses"))?,
            nation: self.nation.ok_or_else(|| E::missing_field("nation"))?,
            tier: self.tier.ok_or_else(|| E::missing_field("tier"))?,
            strategies: self
                .strategies
                .ok_or_else(|| E::missing_field("strategies"))?,
            keywords: self.keywords.ok_or_else(|| E::missing_field("keywords"))?,
        })
    }
}

#[cfg(test)]
mod tests {
    use super::{EdgeType, NodeType, TankGraph, TankRecord};

    #[test]
    fn node_equality_is_variant_sensitive() {
        assert_ne!(
            NodeType::Class("sniping".to_owned()),
            NodeType::Keyword("sniping".to_owned())
        );
        assert_eq!(
            NodeType::Keyword("sniping".to_owned()),
            NodeType::Keyword("sniping".to_owned())
        );
    }

    #[test]
    fn tank_equality_includes_every_field() {
        let left = NodeType::Tank {
            name: "A".to_owned(),
            display_name: "Alpha".to_owned(),
            nation: "USA".to_owned(),
            tier: 5,
        };
        let right = NodeType::Tank {
            name: "A".to_owned(),
            display_name: "Alpha".to_owned(),
            nation: "Germany".to_owned(),
            tier: 5,
        };
        assert_ne!(left, right);
    }

    #[test]
    fn edge_types_support_equality() {
        assert_eq!(EdgeType::IsAClassOf, EdgeType::IsAClassOf);
        assert_ne!(EdgeType::IsAClassOf, EdgeType::IsSubclassOf);
    }

    #[test]
    fn graph_type_alias_is_available() {
        let _ = std::any::type_name::<TankGraph>();
        let _ = std::any::type_name::<TankRecord>();
    }
}
