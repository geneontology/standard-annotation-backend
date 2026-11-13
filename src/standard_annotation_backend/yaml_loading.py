"""Load YAML documents strictly, rejecting mapping keys that repeat.

PyYAML keeps the last value when a mapping repeats a key, so a mistake in a
hand-edited file can silently replace an earlier value. Every YAML document SABE
reads from go-site or from `config/sources.yaml` is loaded here, so a repeated key
fails instead.
"""

import yaml
from yaml.nodes import MappingNode, Node


class _UniqueKeySafeLoader(yaml.SafeLoader):
    """Reject mapping keys that would otherwise silently overwrite earlier values."""

    def construct_mapping(self, node: Node, deep: bool = False) -> dict[object, object]:
        """Build a mapping after rejecting duplicate or unhashable keys.

        Args:
            node: YAML node representing the mapping.
            deep: Whether child objects should also be constructed deeply.

        Returns:
            Parsed mapping with unique keys.

        Raises:
            yaml.YAMLError: If a key is duplicated or cannot be compared.
        """
        if isinstance(node, MappingNode):
            self.flatten_mapping(node)
            seen: set[object] = set()
            for key_node, _ in node.value:
                key = self.construct_object(key_node, deep=deep)
                try:
                    if key in seen:
                        raise yaml.YAMLError("duplicate mapping key")
                    seen.add(key)
                except TypeError:
                    raise yaml.YAMLError("invalid mapping key") from None
        return super().construct_mapping(node, deep=deep)


def load_yaml(text: str) -> object:
    """Parse one YAML document with PyYAML's safe loader, rejecting repeated keys.

    Args:
        text: The complete YAML document.

    Returns:
        The parsed value: a mapping, a list, a scalar, or `None` for an empty
        document.

    Raises:
        yaml.YAMLError: If the text is not valid YAML, uses a tag the safe loader
            does not allow, or repeats a key within one mapping.
    """
    return yaml.load(text, Loader=_UniqueKeySafeLoader)
