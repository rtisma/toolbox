package com.github.rtisma.toolbox.file_renamer.cli.config;

import static picocli.CommandLine.Model.UsageMessageSpec.*;

import com.github.rtisma.toolbox.file_renamer.cli.command.RootCommand;
import com.github.rtisma.toolbox.file_renamer.cli.util.CommandListRenderer;
import com.github.rtisma.toolbox.file_renamer.cli.util.ProjectBanner;
import lombok.NonNull;
import lombok.RequiredArgsConstructor;
import lombok.val;
import picocli.CommandLine;

@RequiredArgsConstructor
public class CliConfig {

  public static final String APPLICATION_NAME = "File-Renamer";

  private static final String HELP_HEADER_GUIDE_URLS = "Help Menu";

  //@NonNull private final CommandLine.IFactory factory; // auto-configured to inject PicocliSpringFactory
  @NonNull private final RootCommand rootCommand; // auto-configured to inject PicocliSpringFactory

  public CommandLine commandLine() {
    //val cmd = new CommandLine(rootCommand, factory);
    val cmd = new CommandLine(rootCommand);
    val banner = new ProjectBanner(APPLICATION_NAME, "@|bold,green ", "|@");
    cmd.getHelpSectionMap().put(SECTION_KEY_HEADER, (x) -> HELP_HEADER_GUIDE_URLS);
    cmd.getHelpSectionMap().put(SECTION_KEY_COMMAND_LIST, new CommandListRenderer());
    addBannerToHelp(cmd, banner);
    return cmd;
  }

  private void addBannerToHelp(CommandLine cmd, ProjectBanner banner) {
    cmd.getHelpSectionMap()
        .put(SECTION_KEY_HEADER_HEADING, help -> help.createHeading(banner.generateBannerText()));
    if (!cmd.getSubcommands().isEmpty()) {
      cmd.getSubcommands().values().forEach(x -> addBannerToHelp(x, banner));
    }
  }
}
