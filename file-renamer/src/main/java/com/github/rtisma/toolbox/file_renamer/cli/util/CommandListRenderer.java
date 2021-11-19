package com.github.rtisma.toolbox.file_renamer.cli.util;

import static picocli.CommandLine.Help.Column.Overflow.SPAN;
import static picocli.CommandLine.Help.Column.Overflow.WRAP;
import static picocli.CommandLine.Help.TextTable.forColumns;

import java.util.HashSet;
import java.util.Set;
import lombok.val;
import picocli.CommandLine;
import picocli.CommandLine.Help;
import picocli.CommandLine.Help.Column;
import picocli.CommandLine.Help.TextTable;
import picocli.CommandLine.IHelpSectionRenderer;
import picocli.CommandLine.Model.UsageMessageSpec;

/** Expands recursively displays all the commands and subcommands in the help menu */
public class CommandListRenderer implements IHelpSectionRenderer {

  @Override
  public String render(Help help) {
    val spec = help.commandSpec();
    if (spec.subcommands().isEmpty()) {
      return "";
    }

    // prepare layout: two columns
    // the left column overflows, the right column wraps if text is too long
    val textTable =
        forColumns(
            help.colorScheme(),
            new Column(15, 2, SPAN),
            new Column(spec.usageMessage().width() - 15, 2, WRAP));
    textTable.setAdjustLineBreaksForWideCJKCharacters(
        spec.usageMessage().adjustLineBreaksForWideCJKCharacters());

    val visitedSubcommands = new HashSet<String>();

    for (CommandLine subcommand : spec.subcommands().values()) {
      addHierarchy(subcommand, textTable, "", visitedSubcommands);
    }
    return textTable.toString();
  }

  private void addHierarchy(
      CommandLine cmd, TextTable textTable, String indent, Set<String> visitedSubcommands) {
    // create comma-separated list of command name and aliases
    val name = cmd.getCommandSpec().name();
    if (!visitedSubcommands.contains(name)) {
      visitedSubcommands.add(name);
      String names = cmd.getCommandSpec().names().toString();
      names = names.substring(1, names.length() - 1); // remove leading '[' and trailing ']'

      // command description is taken from header or description
      String description = description(cmd.getCommandSpec().usageMessage());

      // add a line for this command to the layout
      textTable.addRowValues(indent + names, description);

      // add its subcommands (if any)
      for (CommandLine sub : cmd.getSubcommands().values()) {
        addHierarchy(sub, textTable, indent + "  ", visitedSubcommands);
      }
    }
  }

  private String description(UsageMessageSpec usageMessage) {
    if (usageMessage.header().length > 0) {
      return usageMessage.header()[0];
    }
    if (usageMessage.description().length > 0) {
      return usageMessage.description()[0];
    }
    return "";
  }
}
