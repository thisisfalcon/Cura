# Copyright (c) 2017 Ultimaker B.V.
# Cura is released under the terms of the LGPLv3 or higher.

import copy
import json
import re
from typing import Any, TYPE_CHECKING

from UM.Mesh.MeshWriter import MeshWriter
from UM.Logging.Logger import Logger
from UM.Settings.InstanceContainer import InstanceContainer

from cura.Machines.QualityManager import getMachineDefinitionIDForQualitySearch

if TYPE_CHECKING:
    from UM.Application import Application


__all__ = ["GCodeWriter"]


class GCodeWriter(MeshWriter):
    version = 3

    escape_characters = {
        re.escape("\\"): "\\\\",  # The escape character.
        re.escape("\n"): "\\n",   # Newlines. They break off the comment.
        re.escape("\r"): "\\r"    # Carriage return. Windows users may need this for visualisation in their editors.
    }

    _setting_keyword = ";SETTING_"

    def __init__(self, application: "Application", *args, **kwargs) -> None:
        super().__init__()
        self._application = application

        self._supported_extensions.append(".gcode")

    def write(self, stream, nodes, mode = MeshWriter.OutputMode.TextMode, *args, **kwargs) -> Any:
        if mode != MeshWriter.OutputMode.TextMode:
            Logger.log("e", "GCodeWriter does not support non-text mode.")
            return False

        active_machine = self._application.getMachineManager().getActiveMachine()

        active_build_plate = 0
        scene = self._application.getController().getScene()
        if not hasattr(scene, "gcode_dict"):
            return False
        gcode_dict = getattr(scene, "gcode_dict")
        gcode_list = gcode_dict.get(active_build_plate, None)
        if gcode_list is not None:
            has_settings = False
            for gcode in gcode_list:
                if gcode[:len(self._setting_keyword)] == self._setting_keyword:
                    has_settings = True
                stream.write(gcode)
            # Serialise the current container stack and put it at the end of the file.
            if not has_settings:
                settings = self._serialiseSettings(active_machine.global_stack)
                stream.write(settings)
            return True

        return False

    def _createFlattenedContainerInstance(self, instance_container1, instance_container2):
        flat_container = InstanceContainer(instance_container2.getName())

        # The metadata includes id, name and definition
        flat_container.setMetaData(copy.deepcopy(instance_container2.getMetaData()))

        if instance_container1.getDefinition():
            flat_container.setDefinition(instance_container1.getDefinition().getId())

        for key in instance_container2.getAllKeys():
            flat_container.setProperty(key, "value", instance_container2.getProperty(key, "value"))

        for key in instance_container1.getAllKeys():
            flat_container.setProperty(key, "value", instance_container1.getProperty(key, "value"))

        return flat_container

    def _serialiseSettings(self, stack):
        container_registry = self._application.getContainerRegistry()
        quality_manager = self._application.getQualityManager()

        prefix = self._setting_keyword + str(GCodeWriter.version) + " "  # The prefix to put before each line.
        prefix_length = len(prefix)

        quality_type = stack.quality.getMetaDataEntry("quality_type")
        container_with_profile = stack.qualityChanges
        if container_with_profile.getId() == "empty_quality_changes":
            # If the global quality changes is empty, create a new one
            quality_name = container_registry.uniqueName(stack.quality.getName())
            container_with_profile = quality_manager.createQualityChangesContainer(quality_type, quality_name, stack, None)

        flat_global_container = self._createFlattenedContainerInstance(stack.userChanges, container_with_profile)
        # If the quality changes is not set, we need to set type manually
        if flat_global_container.getMetaDataEntry("type", None) is None:
            flat_global_container.addMetaDataEntry("type", "quality_changes")

        # Ensure that quality_type is set. (Can happen if we have empty quality changes).
        if flat_global_container.getMetaDataEntry("quality_type", None) is None:
            flat_global_container.addMetaDataEntry("quality_type", stack.quality.getMetaDataEntry("quality_type", "normal"))

        # Get the machine definition ID for quality profiles
        machine_definition_id_for_quality = getMachineDefinitionIDForQualitySearch(stack.definition)
        flat_global_container.setMetaDataEntry("definition", machine_definition_id_for_quality)

        serialized = flat_global_container.serialize()
        data = {"global_quality": serialized}

        all_setting_keys = set(flat_global_container.getAllKeys())
        for extruder in sorted(stack.extruders.values(), key = lambda k: int(k.getMetaDataEntry("position"))):
            extruder_quality = extruder.qualityChanges
            if extruder_quality.getId() == "empty_quality_changes":
                # Same story, if quality changes is empty, create a new one
                quality_name = container_registry.uniqueName(stack.quality.getName())
                extruder_quality = quality_manager.createQualityChangesContainer(quality_type, quality_name, stack, None)

            flat_extruder_quality = self._createFlattenedContainerInstance(extruder.userChanges, extruder_quality)
            # If the quality changes is not set, we need to set type manually
            if flat_extruder_quality.getMetaDataEntry("type", None) is None:
                flat_extruder_quality.addMetaDataEntry("type", "quality_changes")

            # Ensure that extruder is set. (Can happen if we have empty quality changes).
            if flat_extruder_quality.getMetaDataEntry("position", None) is None:
                flat_extruder_quality.addMetaDataEntry("position", extruder.getMetaDataEntry("position"))

            # Ensure that quality_type is set. (Can happen if we have empty quality changes).
            if flat_extruder_quality.getMetaDataEntry("quality_type", None) is None:
                flat_extruder_quality.addMetaDataEntry("quality_type", extruder.quality.getMetaDataEntry("quality_type", "normal"))

            # Change the default definition
            flat_extruder_quality.setMetaDataEntry("definition", machine_definition_id_for_quality)

            extruder_serialized = flat_extruder_quality.serialize()
            data.setdefault("extruder_quality", []).append(extruder_serialized)

            all_setting_keys.update(set(flat_extruder_quality.getAllKeys()))

        # Check if there is any profiles
        if not all_setting_keys:
            Logger.log("i", "No custom settings found, not writing settings to g-code.")
            return ""

        json_string = json.dumps(data)

        # Escape characters that have a special meaning in g-code comments.
        pattern = re.compile("|".join(GCodeWriter.escape_characters.keys()))

        # Perform the replacement with a regular expression.
        escaped_string = pattern.sub(lambda m: GCodeWriter.escape_characters[re.escape(m.group(0))], json_string)

        # Introduce line breaks so that each comment is no longer than 80 characters. Prepend each line with the prefix.
        result = ""

        # Lines have 80 characters, so the payload of each line is 80 - prefix.
        for pos in range(0, len(escaped_string), 80 - prefix_length):
            result += prefix + escaped_string[pos: pos + 80 - prefix_length] + "\n"
        return result